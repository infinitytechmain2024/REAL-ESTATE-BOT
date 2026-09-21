"""Read group access and posts using selectors checked against a live Russian UI.

Callers own FacebookSession.lock throughout navigation and extraction.
Unknown layouts fail visibly; reading never joins a group.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import quote, urljoin

from playwright.async_api import Locator, Page
from pydantic import BaseModel, Field

from bot.logging_conf import get_logger
from bot.services.facebook.discovery import _group_and_post

log = get_logger(__name__)

# Facebook uses the account's locale, independently of the browser locale.
JOIN_SIGNALS = ("Join Group", "Join group", "Unirse al grupo", "Unirte al grupo",
                "Присоединиться к группе")
PENDING_SIGNALS = ("Pending", "Pendiente", "Отменить запрос")
UNAVAILABLE_SIGNALS = ("isn't available", "no está disponible", "no esta disponible",
                       "Этот контент сейчас недоступен")
COMPOSER_SIGNALS = ("Write something", "Escribe algo", "Напишите что-нибудь")
SEARCH_PLACEHOLDERS = ("Search this group", "Buscar en este grupo", "Поиск в этой группе")
SEARCH_BUTTONS = ("Search this group", "Buscar en este grupo", "Поиск по этой группе")
SEE_MORE_SIGNALS = ("See more", "Ver más", "Ver mas", "Ещё")
SEE_LESS_SIGNALS = ("See less", "Ver menos", "Показать меньше")
EMPTY_SIGNALS = ("No results found", "No se encontraron resultados", "Ничего не найдено")
POST_LINKS = "a[href*='/posts/'], a[href*='/permalink/']"
FEED_LINKS = "div[role='article'] a[href*='/posts/'], div[role='article'] a[href*='/permalink/']"
MESSAGE_SELECTOR = "[data-ad-preview='message'], [data-ad-comet-preview='message']"
MESSAGE_FALLBACK_SELECTOR = "[dir='auto']"


async def discover_groups(page: Page, query: str, *, max_groups: int) -> list[tuple[str, str]]:
    """Find public groups in Facebook's own group search, without joining."""
    await page.goto(
        "https://www.facebook.com/search/groups/?q=" + quote(query),
        wait_until="domcontentloaded",
    )
    await page.wait_for_selector("div[role='main'] a[href*='/groups/']", timeout=15000)
    found: dict[str, str] = {}
    links = page.locator("div[role='main'] a[href*='/groups/']")
    for index in range(await links.count()):
        link = links.nth(index)
        href = await link.get_attribute("href")
        parsed = _group_and_post(urljoin("https://www.facebook.com/", href or ""))
        if parsed is None or parsed[1] is not None:
            continue
        group_url, _ = parsed
        title = (await link.inner_text()).strip()
        if title and group_url not in found:
            found[group_url] = title
        if len(found) >= max_groups:
            break
    return list(found.items())

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

    # React can render the group header before the feed. Allow a short bounded
    # render window before treating a Join button as an actual access restriction.
    for attempt in range(4):
        if "login" in page.url or await page.locator(LOGIN_FORM_SELECTOR).count():
            return GroupAccess.LOGIN_REQUIRED
        if await _any_text(page, UNAVAILABLE_SIGNALS):
            return GroupAccess.UNAVAILABLE
        if (await page.locator(FEED_LINKS).count()
                or await _first_placeholder(page, SEARCH_PLACEHOLDERS) is not None
                or await _any_text(page, COMPOSER_SIGNALS)):
            return GroupAccess.ACCESSIBLE
        if attempt < 3:
            await page.wait_for_timeout(1000)

    # Membership and read access differ: public posts remain readable while a
    # Join button or pending membership request is present.
    if await _any_text(page, PENDING_SIGNALS):
        return GroupAccess.PENDING_APPROVAL
    if await _any_text(page, JOIN_SIGNALS):
        return GroupAccess.MEMBERSHIP_REQUIRED
    log.warning("facebook.group.unrecognised_layout", group_url=group_url, url=page.url)
    return GroupAccess.UNKNOWN_ERROR


async def join_group(page: Page) -> bool:
    """Click a normal public Join button; never handle challenges or questions."""
    for signal in JOIN_SIGNALS:
        button = page.get_by_role("button", name=signal, exact=True)
        if await button.count():
            await button.first.click()
            await page.wait_for_timeout(1000)
            return True
    return False


async def search_posts(
    page: Page, group_url: str, query: str, *, max_posts: int
) -> list[GroupPost]:
    """Search and collect real posts, ignoring people cards and UI text."""
    search_box = await _first_placeholder(page, SEARCH_PLACEHOLDERS)
    if search_box is None:
        for name in SEARCH_BUTTONS:
            button = page.get_by_role("button", name=name, exact=True)
            if await button.count():
                await button.first.click()
                # The search input is inserted only after the button is opened.
                await page.wait_for_selector("input[type='search']", timeout=10000)
                search_box = await _first_placeholder(page, SEARCH_PLACEHOLDERS)
                break
    if search_box is None:
        log.warning("facebook.group.no_search_box", group_url=group_url)
        raise RuntimeError("Facebook group search box unavailable")

    await search_box.first.click()
    await search_box.first.fill(query)
    await page.keyboard.press("Enter")
    # A caller may already be on the group's search page (as happens after a
    # failed extraction retry); in that case Facebook updates results in place
    # and emits no navigation event.
    if "/search" not in page.url:
        await page.wait_for_url("**/search**", timeout=15000)
    await page.wait_for_selector("div[role='article']", timeout=15000)

    posts: list[GroupPost] = []
    seen_urls: set[str] = set()
    stagnant_rounds = 0

    while len(posts) < max_posts and stagnant_rounds < 3:
        before = len(posts)
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

        if len(posts) >= max_posts or await _any_text(page, EMPTY_SIGNALS):
            break
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(1200)
        stagnant_rounds = stagnant_rounds + 1 if len(posts) == before else 0

    if not posts and not await _any_text(page, EMPTY_SIGNALS):
        raise RuntimeError("Facebook search returned no readable posts or explicit empty state")

    log.info("facebook.group.search_posts", group_url=group_url, query=query, found=len(posts))
    return posts


async def read_recent_posts(
    page: Page, group_url: str, *, max_posts: int
) -> list[GroupPost]:
    """Read the newest visible feed posts when in-group keyword search misses."""
    posts: list[GroupPost] = []
    seen_urls: set[str] = set()
    stagnant_rounds = 0
    while len(posts) < max_posts and stagnant_rounds < 2:
        before = len(posts)
        articles = page.locator("div[role='article']")
        for index in range(await articles.count()):
            if len(posts) >= max_posts:
                break
            try:
                post = await _extract_post(articles.nth(index), group_url)
            except RuntimeError:
                continue
            if post is None or post.post_url in seen_urls:
                continue
            seen_urls.add(post.post_url)
            posts.append(post)
        if len(posts) >= max_posts:
            break
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(1000)
        stagnant_rounds = stagnant_rounds + 1 if len(posts) == before else 0
    log.info("facebook.group.recent_posts", group_url=group_url, found=len(posts))
    return posts


async def _extract_post(article: Locator, group_url: str) -> GroupPost | None:
    """Read only a post's message, author and permalink, never comments/UI text."""
    links = article.locator(POST_LINKS)
    if not await links.count():
        return None  # People cards also have role=article in group search.
    link = links.first
    href = await link.get_attribute("href")
    canonical = _group_and_post(urljoin("https://www.facebook.com/", href or ""))
    if canonical is None or canonical[1] is None:
        raise RuntimeError("Facebook post has no usable permalink")
    # A feed can contain a recommended or cross-posted article whose permalink
    # belongs to another group.  Keep the source scoped to the group we opened;
    # otherwise a valid-looking Facebook URL leaks an unrelated result into the
    # user's search.
    expected_group = _group_and_post(urljoin("https://www.facebook.com/", group_url))
    if expected_group is not None and canonical[0] != expected_group[0]:
        return None

    message = article.locator(MESSAGE_SELECTOR).first
    if not await message.count():
        # Feed cards sometimes omit the data-ad marker entirely. Their first
        # non-empty dir=auto span is the post body; later spans are translation
        # labels or nested comments.
        candidate = article.locator(MESSAGE_FALLBACK_SELECTOR).first
        candidate_text = (await candidate.inner_text()).strip()
        if candidate_text and candidate_text not in SEE_MORE_SIGNALS + SEE_LESS_SIGNALS:
            message = candidate
    if not await message.count():
        # A post permalink without a message is a broken extraction, whereas
        # an article without a permalink was already filtered as a people card.
        raise RuntimeError("Facebook post message unavailable")
    for signal in SEE_MORE_SIGNALS:
        more = message.get_by_text(signal, exact=True)
        if await more.count():
            await more.first.click()
            break
    text = (await message.inner_text()).strip()
    for suffix in SEE_LESS_SIGNALS:
        text = text.removesuffix(suffix).strip()
    if not text:
        raise RuntimeError("Facebook post message is empty")

    author = None
    authors = article.locator("a[href*='/user/']")
    for index in range(await authors.count()):
        name = (await authors.nth(index).inner_text()).strip()
        if name:
            author = name
            break
    return GroupPost(
        group_url=group_url, post_url=canonical[1], author=author, text=text,
        posted_at_text=(await link.inner_text()).strip() or None,
    )
