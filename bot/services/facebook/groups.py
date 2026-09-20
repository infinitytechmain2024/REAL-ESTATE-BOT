"""Reading a Facebook group: access classification and post/comment extraction.

Nothing here is validated against a real group yet -- run
``scripts/facebook_probe.py`` against a real, accessible group before
trusting any of this. Facebook's markup varies by locale, account and
rollout, so the selectors here are a documented starting point, not a
contract. Log generously and compare a handful of results against the
browser by hand before relying on this for anything.
"""

from __future__ import annotations

import contextlib
from enum import StrEnum

from playwright.async_api import Locator, Page
from pydantic import BaseModel, Field

from bot.logging_conf import get_logger

log = get_logger(__name__)

# Facebook renders in the *account's* language, not the browser's, and in
# CDP-attach mode we do not control either. Every visible-text probe below
# therefore carries its Spanish variant beside the English one -- Spain is
# the v1 market, so Spanish is the likely default, not the exception.
#
# Accents are a real hazard here: a group rendered with "Ver más" and one
# rendered "Ver mas" are the same button, so both spellings are listed rather
# than relying on normalisation Facebook does not promise.
JOIN_SIGNALS = ("Join Group", "Join group", "Unirse al grupo", "Unirte al grupo")
PENDING_SIGNALS = ("Pending", "Pendiente")
UNAVAILABLE_SIGNALS = ("isn't available", "no está disponible", "no esta disponible")
COMPOSER_SIGNALS = ("Write something", "Escribe algo")
SEARCH_PLACEHOLDERS = ("Search this group", "Buscar en este grupo")
SEE_MORE_SIGNALS = ("See more", "Ver más", "Ver mas")

# Structural: a login form is a login form in any language.
LOGIN_FORM_SELECTOR = "#login_form, form[data-testid='royal_login_form']"


async def _any_text(page: Page, signals: tuple[str, ...]) -> bool:
    """Whether any localized variant of *signals* is visible on *page*."""
    for signal in signals:
        if await page.get_by_text(signal, exact=False).count() > 0:
            return True
    return False


async def _first_placeholder(page: Page, signals: tuple[str, ...]) -> Locator | None:
    """The first input whose placeholder matches any variant, or None."""
    for signal in signals:
        locator = page.get_by_placeholder(signal, exact=False)
        if await locator.count() > 0:
            return locator
    return None


class GroupAccess(StrEnum):
    """What the group looked like when we tried to open it.

    ``UNKNOWN_ERROR`` and ``UNAVAILABLE`` are deliberately distinct from
    ``ACCESSIBLE`` / ``MEMBERSHIP_REQUIRED``: a failed extraction is not
    evidence the group is gone or inactive, and must never be reported as
    one -- see the implementation plan on distinguishing extraction failure
    from a genuinely inaccessible or quiet group.
    """

    ACCESSIBLE = "accessible"
    MEMBERSHIP_REQUIRED = "membership_required"
    PENDING_APPROVAL = "pending_approval"
    LOGIN_REQUIRED = "login_required"
    UNAVAILABLE = "unavailable"
    UNKNOWN_ERROR = "unknown_error"


class GroupComment(BaseModel):
    """One comment read from a post."""

    post_url: str
    comment_url: str | None = None
    author: str | None = None
    author_profile_url: str | None = None
    text: str = ""


class GroupPost(BaseModel):
    """One post read from a group feed or a group's own search results."""

    group_url: str
    post_url: str
    author: str | None = None
    text: str = ""
    posted_at_text: str | None = Field(
        default=None, description="Facebook's own relative/absolute timestamp text, unparsed"
    )
    comments: list[GroupComment] = Field(default_factory=list)


async def check_access(page: Page, group_url: str) -> GroupAccess:
    """Open *group_url* and classify what we can see, without reading anything yet."""
    try:
        response = await page.goto(group_url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001 - a nav failure is "unknown", not fatal to the caller
        log.warning("facebook.group.nav_failed", group_url=group_url, error=str(exc))
        return GroupAccess.UNKNOWN_ERROR

    if response is not None and response.status in (404, 410):
        return GroupAccess.UNAVAILABLE

    # Structural signals first: the URL and the login form mean the same
    # thing in every market, so they are trusted ahead of any visible text.
    if "login" in page.url or await page.locator(LOGIN_FORM_SELECTOR).count():
        return GroupAccess.LOGIN_REQUIRED

    if await _any_text(page, JOIN_SIGNALS):
        return GroupAccess.MEMBERSHIP_REQUIRED
    if await _any_text(page, PENDING_SIGNALS):
        return GroupAccess.PENDING_APPROVAL
    if await _any_text(page, UNAVAILABLE_SIGNALS):
        return GroupAccess.UNAVAILABLE

    # A post composer or the group's own search box is the strongest signal
    # of real membership; anything else is a guess and stays UNKNOWN_ERROR --
    # which is not the same as "empty" or "inactive", and must never be
    # reported as one.
    if await _first_placeholder(page, SEARCH_PLACEHOLDERS) is not None:
        return GroupAccess.ACCESSIBLE
    if await _any_text(page, COMPOSER_SIGNALS):
        return GroupAccess.ACCESSIBLE

    log.warning("facebook.group.unrecognised_layout", group_url=group_url, url=page.url)
    return GroupAccess.UNKNOWN_ERROR


async def search_posts(
    page: Page, group_url: str, query: str, *, max_posts: int
) -> list[GroupPost]:
    """Search *query* inside an already-accessible group and read up to *max_posts*.

    This is the single riskiest piece of the whole project to get right --
    the group's own search UI, how results paginate, and how much text a
    post needs "See more" expanded before it is complete all vary. Treat the
    first real run of this as a validation step, not a working feature.
    """
    search_box = await _first_placeholder(page, SEARCH_PLACEHOLDERS)
    if search_box is None:
        log.warning("facebook.group.no_search_box", group_url=group_url)
        return []

    await search_box.first.click()
    await search_box.first.fill(query)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state("networkidle")

    posts: list[GroupPost] = []
    seen_urls: set[str] = set()
    stagnant_rounds = 0

    while len(posts) < max_posts and stagnant_rounds < 3:
        articles = page.locator("div[role='article']")
        count = await articles.count()
        for i in range(count):
            if len(posts) >= max_posts:
                break
            post = await _extract_post(articles.nth(i), group_url)
            if post is None or post.post_url in seen_urls:
                continue
            seen_urls.add(post.post_url)
            posts.append(post)

        before = len(posts)
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(1200)
        stagnant_rounds = stagnant_rounds + 1 if len(posts) == before else 0

    log.info("facebook.group.search_posts", group_url=group_url, query=query, found=len(posts))
    return posts


async def _extract_post(article: Locator, group_url: str) -> GroupPost | None:
    """Best-effort extraction of one feed article.

    Returns ``None`` if it does not look like a real post (no permalink
    found) rather than returning a half-populated, misleading record.
    """
    for signal in SEE_MORE_SIGNALS:
        see_more = article.get_by_text(signal, exact=False)
        if await see_more.count() > 0:
            with contextlib.suppress(Exception):  # expanding is an optimisation, not required
                await see_more.first.click()
            break

    text = (await article.inner_text()).strip()
    if not text:
        return None

    link = article.locator(
        "a[href*='/posts/'], a[href*='/permalink/'], a[href*='?story_fbid=']"
    )
    href = await link.first.get_attribute("href") if await link.count() > 0 else None
    if not href:
        return None

    return GroupPost(group_url=group_url, post_url=href, text=text)
