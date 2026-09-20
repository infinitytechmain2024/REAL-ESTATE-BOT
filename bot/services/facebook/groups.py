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
    if "login" in page.url or await page.locator(
        "#login_form, form[data-testid='royal_login_form']"
    ).count():
        return GroupAccess.LOGIN_REQUIRED

    if await page.get_by_text("Join Group", exact=False).count() > 0:
        return GroupAccess.MEMBERSHIP_REQUIRED
    if await page.get_by_text("Pending", exact=False).count() > 0:
        return GroupAccess.PENDING_APPROVAL
    if await page.get_by_text("isn't available", exact=False).count() > 0:
        return GroupAccess.UNAVAILABLE

    # A post composer or the feed's own search box is the strongest signal of
    # real membership; anything else is a guess and stays UNKNOWN_ERROR.
    if await page.get_by_placeholder("Search this group", exact=False).count() > 0:
        return GroupAccess.ACCESSIBLE
    if await page.get_by_text("Write something", exact=False).count() > 0:
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
    search_box = page.get_by_placeholder("Search this group", exact=False)
    if await search_box.count() == 0:
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
    see_more = article.get_by_text("See more", exact=False)
    if await see_more.count() > 0:
        with contextlib.suppress(Exception):  # expanding text is an optimisation, not required
            await see_more.first.click()

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
