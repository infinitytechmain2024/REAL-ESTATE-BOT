"""Per-platform search URLs, result parsing and block detection.

The Browser Session Manager navigates and returns a snapshot: the page's URL,
title and text, the raw result cards (``items``: href or LinkedIn URN, card
text, image alt, author line, time), Open Graph fields (``meta``) and
``logged_in`` (whether the platform's login cookie is present). Everything
here is pure: it builds URLs, validates and normalises every candidate to one
canonical URL and id, and recognises login walls, captchas, checkpoints and
rate limits. Nothing here navigates.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote, quote_plus, unquote

SOCIAL_PLATFORMS: tuple[str, ...] = ("tiktok", "instagram", "linkedin")
PLATFORM_NAMES = {"tiktok": "TikTok", "instagram": "Instagram", "linkedin": "LinkedIn"}
MAX_TEXT = 4_000
SHORT_TEXT = 40  # a card with less text than this is opened (when the adapter can) to read its caption

ItemKind = Literal["post", "person", "company"]
BlockKind = Literal["login", "captcha", "checkpoint", "rate_limit"]


@dataclass(frozen=True, slots=True)
class SocialItem:
    """One post or profile a search showed, normalised."""

    platform: str
    kind: ItemKind
    key: str  # normalised id, unique per platform: "video:7301…", "p:Cx1…", "activity:7…", "in:ana-lopez"
    url: str  # canonical https URL
    text: str
    author: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Block:
    """The page is not results: a login wall, captcha, checkpoint or rate limit."""

    kind: BlockKind
    reason: str


def _squash(value: object, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _merge_text(*parts: object) -> str:
    seen: list[str] = []
    for part in parts:
        text = _squash(part)
        if text and not any(text in other for other in seen):
            seen.append(text)
    return _squash(" · ".join(seen))


def _epoch(seconds: float) -> datetime | None:
    """A timestamp decoded from an id, only when it is plausible (2012 .. tomorrow)."""
    try:
        moment = datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment if datetime(2012, 1, 1, tzinfo=UTC) <= moment <= datetime.now(UTC) + timedelta(days=1) else None


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _hashtag(text: str) -> str:
    return re.sub(r"[^\w]", "", text.strip().lstrip("#"), flags=re.UNICODE).lower()


class Adapter:
    platform: str = ""
    kinds: tuple[str, ...] = ()
    detail: bool = False  # can a single post page be read for its caption, author and date

    def search_url(self, kind: str, query: str) -> str:
        raise NotImplementedError

    def item(self, raw: Mapping[str, object], kind: str) -> SocialItem | None:
        raise NotImplementedError

    def parse(self, snapshot: Mapping[str, object], kind: str, *, limit: int = 40) -> list[SocialItem]:
        """The distinct, valid result items of a search page, in page order, at most ``limit``."""
        raw_items = snapshot.get("items")
        found: dict[str, SocialItem] = {}
        for raw in raw_items if isinstance(raw_items, Sequence) and not isinstance(raw_items, str) else []:
            if not isinstance(raw, Mapping):
                continue
            item = self.item(raw, kind)
            if item is None:
                continue
            known = found.get(item.key)
            if known is None:
                if len(found) >= limit:
                    continue
                found[item.key] = item
            elif len(item.text) > len(known.text):  # the same post twice (image + caption links): keep the richer card
                found[item.key] = replace(item, published_at=item.published_at or known.published_at)
        return list(found.values())

    def needs_detail(self, item: SocialItem) -> bool:
        return self.detail and item.kind == "post" and len(item.text) < SHORT_TEXT

    def with_detail(self, item: SocialItem, snapshot: Mapping[str, object]) -> SocialItem:
        return item


# --- TikTok ---------------------------------------------------------------------------------

_TIKTOK_POST = re.compile(r"^https?://(?:www\.|m\.)?tiktok\.com/@([A-Za-z0-9._-]{1,64})/(video|photo)/(\d{8,25})(?:[/?#].*)?$")


class TikTokAdapter(Adapter):
    platform, kinds, detail = "tiktok", ("hashtag", "keyword"), True

    def search_url(self, kind: str, query: str) -> str:
        if kind == "hashtag":
            return f"https://www.tiktok.com/tag/{quote(_hashtag(query))}"
        if kind == "keyword":
            return f"https://www.tiktok.com/search?q={quote_plus(query)}"
        raise ValueError(f"unsupported TikTok query kind: {kind}")

    def item(self, raw: Mapping[str, object], kind: str) -> SocialItem | None:
        match = _TIKTOK_POST.match(str(raw.get("url") or ""))
        if not match:
            return None
        user, media, video_id = match.groups()
        return SocialItem(
            "tiktok", "post", f"video:{video_id}", f"https://www.tiktok.com/@{user}/{media}/{video_id}",
            _merge_text(raw.get("text"), raw.get("alt")), f"@{user}",
            # A TikTok id carries its creation second in the top 32 bits.
            _epoch(int(video_id) >> 32),
        )

    def with_detail(self, item: SocialItem, snapshot: Mapping[str, object]) -> SocialItem:
        meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), Mapping) else {}
        assert isinstance(meta, Mapping)
        text = _merge_text(item.text, meta.get("og_description") or meta.get("description"))
        return replace(item, text=text)


# --- Instagram ------------------------------------------------------------------------------

_INSTAGRAM_POST = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9._]{1,30}/)?(p|reel|reels|tv)/([A-Za-z0-9_-]{5,40})/?(?:[?#].*)?$")
# og:description of a post page: "12 likes, 3 comments - casas.madrid on March 3, 2025: "Parcela en venta…""
_INSTAGRAM_OG = re.compile(r"^.*?-\s+([A-Za-z0-9._]{1,30})\s+on\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s*:\s*(.*)$", re.DOTALL)


class InstagramAdapter(Adapter):
    platform, kinds, detail = "instagram", ("hashtag", "keyword"), True

    def search_url(self, kind: str, query: str) -> str:
        if kind == "hashtag":
            return f"https://www.instagram.com/explore/tags/{quote(_hashtag(query))}/"
        if kind == "keyword":
            return f"https://www.instagram.com/explore/search/keyword/?q={quote_plus(query)}"
        raise ValueError(f"unsupported Instagram query kind: {kind}")

    def item(self, raw: Mapping[str, object], kind: str) -> SocialItem | None:
        match = _INSTAGRAM_POST.match(str(raw.get("url") or ""))
        if not match:
            return None
        media, code = match.groups()
        path = "reel" if media in {"reel", "reels"} else "p"
        return SocialItem("instagram", "post", f"p:{code}", f"https://www.instagram.com/{path}/{code}/",
                          _merge_text(raw.get("text"), raw.get("alt")), None, _iso(raw.get("time")))

    def needs_detail(self, item: SocialItem) -> bool:
        # A grid tile has no caption, only an image description: the post page has the text.
        return item.kind == "post" and (item.author is None or len(item.text) < SHORT_TEXT)

    def with_detail(self, item: SocialItem, snapshot: Mapping[str, object]) -> SocialItem:
        meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), Mapping) else {}
        assert isinstance(meta, Mapping)
        description = _squash(meta.get("og_description") or meta.get("description"))
        author, published, caption = item.author, item.published_at or _iso(meta.get("time")), description
        match = _INSTAGRAM_OG.match(description)
        if match:
            author = f"@{match.group(1)}"
            caption = match.group(3).strip().strip('"“”')
            if published is None:
                try:
                    published = datetime.strptime(match.group(2), "%B %d, %Y").replace(tzinfo=UTC)
                except ValueError:
                    published = None
        return replace(item, text=_merge_text(caption, item.text), author=author, published_at=published)


# --- LinkedIn -------------------------------------------------------------------------------

_LINKEDIN_ACTIVITY = re.compile(r"(?:urn:li:activity:|activity-|urn%3Ali%3Aactivity%3A)(\d{15,25})")
_LINKEDIN_HOST = re.compile(r"^https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com/", re.IGNORECASE)
_LINKEDIN_PERSON = re.compile(r"^https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com/in/([^/?#]{2,100})/?(?:[?#].*)?$", re.IGNORECASE)
_LINKEDIN_COMPANY = re.compile(
    r"^https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com/company/([^/?#]{2,100})/?(?:[?#].*)?$", re.IGNORECASE)
_SLUG = re.compile(r"^[\w\-.]{2,100}$", re.UNICODE)
_RESERVED_COMPANY = frozenset({"setup", "admin", "unavailable"})
LINKEDIN_KIND_ITEMS = {"posts": "post", "people": "person", "companies": "company"}


class LinkedInAdapter(Adapter):
    platform, kinds, detail = "linkedin", ("posts", "people", "companies"), False

    def search_url(self, kind: str, query: str) -> str:
        section = {"posts": "content", "people": "people", "companies": "companies"}.get(kind)
        if section is None:
            raise ValueError(f"unsupported LinkedIn query kind: {kind}")
        return f"https://www.linkedin.com/search/results/{section}/?keywords={quote_plus(query)}"

    def item(self, raw: Mapping[str, object], kind: str) -> SocialItem | None:
        want = LINKEDIN_KIND_ITEMS.get(kind)
        url = str(raw.get("url") or "")
        text = _merge_text(raw.get("text"))
        author = _squash(raw.get("author"), 200) or None
        if want == "post":
            if not (url.startswith("urn:li:") or _LINKEDIN_HOST.match(url)):
                return None
            match = _LINKEDIN_ACTIVITY.search(url)
            if not match:
                return None
            activity = match.group(1)
            # A LinkedIn activity id carries its creation millisecond in the top 41 bits.
            return SocialItem("linkedin", "post", f"activity:{activity}",
                              f"https://www.linkedin.com/feed/update/urn:li:activity:{activity}/",
                              text, author, _epoch((int(activity) >> 22) / 1000))
        pattern = _LINKEDIN_PERSON if want == "person" else _LINKEDIN_COMPANY if want == "company" else None
        match = pattern.match(url) if pattern else None
        if not match:
            return None
        slug = unquote(match.group(1)).strip().lower()
        if not _SLUG.match(slug) or (want == "company" and slug in _RESERVED_COMPANY):
            return None
        path = "in" if want == "person" else "company"
        return SocialItem("linkedin", want, f"{path}:{slug}", f"https://www.linkedin.com/{path}/{quote(slug)}/",  # type: ignore[arg-type]
                          text, author, None)


ADAPTERS: dict[str, Adapter] = {a.platform: a for a in (TikTokAdapter(), InstagramAdapter(), LinkedInAdapter())}


def adapter_for(platform: str) -> Adapter:
    try:
        return ADAPTERS[platform]
    except KeyError:
        raise ValueError(f"no social search adapter for {platform!r}") from None


# --- blocks -----------------------------------------------------------------------------------

_URL_BLOCKS: dict[str, tuple[tuple[str, BlockKind], ...]] = {
    "tiktok": (("/login", "login"), ("captcha", "captcha"), ("/verify", "captcha")),
    "instagram": (("/accounts/login", "login"), ("/challenge", "checkpoint"), ("/accounts/suspended", "checkpoint"),
                  ("/accounts/disabled", "checkpoint")),
    "linkedin": (("/authwall", "login"), ("/uas/login", "login"), ("linkedin.com/login", "login"), ("/signup", "login"),
                 ("/checkpoint", "checkpoint")),
}
_TEXT_BLOCKS: tuple[tuple[str, BlockKind], ...] = (
    ("captcha", "captcha"), ("drag the slider", "captcha"), ("verify to continue", "captcha"),
    ("verify you are human", "captcha"), ("i'm not a robot", "captcha"), ("no soy un robot", "captcha"),
    ("security check", "checkpoint"), ("suspicious activity", "checkpoint"), ("unusual activity", "checkpoint"),
    ("confirm it's you", "checkpoint"), ("confirm it’s you", "checkpoint"), ("automated behavior", "checkpoint"),
    ("verify your identity", "checkpoint"), ("account has been restricted", "checkpoint"),
    ("we restrict certain activity", "rate_limit"), ("try again later", "rate_limit"),
    ("too many requests", "rate_limit"), ("maximum number of attempts", "rate_limit"),
    ("you're going too fast", "rate_limit"), ("you’re going too fast", "rate_limit"),
    ("log in to tiktok", "login"), ("log in to instagram", "login"), ("sign in to linkedin", "login"),
    ("join linkedin", "login"), ("inicia sesión en instagram", "login"), ("sign in to view", "login"),
)


def detect_block(platform: str, snapshot: Mapping[str, object]) -> Block | None:
    """A conservative stop signal: the profile is logged out, or the page is a challenge or a rate limit.

    A result page that happens to contain one of the phrases also stops the
    query: stopping is always the safe mistake.
    """
    url = str(snapshot.get("url") or "").lower()
    for signal, kind in _URL_BLOCKS.get(platform, ()):
        if signal in url:
            return Block(kind, f"{platform}_url:{signal}")
    text = f"{snapshot.get('title', '')}\n{snapshot.get('text', '')}".lower()
    for signal, kind in _TEXT_BLOCKS:
        if signal in text:
            return Block(kind, f"{platform}_page:{signal}")
    if snapshot.get("logged_in") is False:
        return Block("login", f"{platform}_cookie:logged_out")
    return None
