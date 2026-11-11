"""A person's engagement with a social post (comment, reaction, repost) and their profile link, per network.

The same shape for Facebook, Instagram, TikTok and LinkedIn, so the investor stages never learn where a person came
from. Only what the provider gave: no name is guessed from a username, a reaction without its type has none, and the
profile is the public link the network itself uses -- nothing here holds cookies, tokens or session data.

``profile_link(platform, href)`` turns a link into ``(key, url)``: ``key`` identifies the person within a campaign
(``fb:<id>``, ``ig:<username>``, ``tt:<uniqueId>``, ``li:in:<id>``, ``li:company:<id>``), ``url`` is the canonical
public profile. Anything that is not a person's (or a page's / company's) own profile -- a post, a hashtag, another
host -- is ``None``.

Light on purpose: no ``bot.campaign`` import, because the images that copy ``bot/acquisition`` alone import it too.
"""

from __future__ import annotations

import re
from typing import Literal, get_args
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import Platform, _Strict

EngagementKind = Literal["comment", "reaction", "repost"]
PLATFORMS: tuple[str, ...] = get_args(Platform)

# --- Facebook (moved from bot.campaign.leads; the same hosts as bot.campaign.fblinks.FB_HOSTS) --------------------

FB_HOSTS = frozenset({"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"})
# First path segments that are never a person or a page.
_FB_NOT_PROFILES = frozenset({
    "groups", "hashtag", "photo", "photo.php", "photos", "watch", "events", "share", "sharer", "sharer.php",
    "reel", "reels", "story.php", "permalink.php", "login", "login.php", "help", "policies", "privacy",
    "marketplace", "gaming", "search", "notifications", "messages", "friends", "bookmarks", "pages", "l.php",
    "home.php", "settings", "ads", "business", "stories", "saved", "memories", "privacy_policy", "legal",
})
_FB_VANITY = re.compile(r"^[A-Za-z0-9.\-]{3,80}$")
_FB_DIGITS = re.compile(r"^\d{5,25}$")

# --- Instagram: instagram.com/<username> ------------------------------------------------------------------------

IG_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})
_IG_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_IG_NOT_PROFILES = frozenset({
    "p", "reel", "reels", "tv", "explore", "stories", "accounts", "direct", "about", "developer", "legal", "web",
    "static", "challenge", "emails", "session", "privacy", "help", "press", "api", "graphql", "locations", "tags",
    "topics", "ar", "lite", "download", "terms", "oauth", "data", "directory", "your_activity",
})

# --- TikTok: tiktok.com/@<uniqueId> -----------------------------------------------------------------------------

TT_HOSTS = frozenset({"tiktok.com", "www.tiktok.com", "m.tiktok.com"})
_TT_HANDLE = re.compile(r"^@([A-Za-z0-9._]{2,24})$")

# --- LinkedIn: linkedin.com/in/<id>, linkedin.com/company/<id> ---------------------------------------------------

_LI_HOST = re.compile(r"^(?:(?:www|[a-z]{2})\.)?linkedin\.com$")
_LI_ID = re.compile(r"^[\w\-]{2,100}$")
_LI_KINDS = {"in": "in", "company": "company"}


def _parts(href: object, hosts: frozenset[str] | re.Pattern[str]) -> SplitResult | None:
    if not isinstance(href, str):
        return None
    parts = urlsplit(href.strip())
    host = (parts.hostname or "").lower()
    ok = hosts.match(host) if isinstance(hosts, re.Pattern) else host in hosts
    if parts.scheme not in ("https", "http") or not ok:
        return None
    return parts


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _facebook(href: object) -> tuple[str, str] | None:
    """Group-scoped member links (``/groups/<g>/user/<id>/``), ``profile.php?id=`` and ``/people/<name>/<id>``
    become ``https://www.facebook.com/profile.php?id=<id>``; a vanity name becomes ``https://www.facebook.com/<name>``.
    Tracking parameters are dropped."""
    parts = _parts(href, FB_HOSTS)
    if parts is None:
        return None
    segments = _segments(parts.path)
    user_id: str | None = None
    if len(segments) >= 4 and segments[0] == "groups" and segments[2] == "user":
        user_id = segments[3]
    elif segments[:1] == ["profile.php"]:
        user_id = (parse_qs(parts.query).get("id") or [""])[0]
    elif len(segments) >= 3 and segments[0] == "people":
        user_id = segments[2]
    if user_id is not None:
        if not _FB_DIGITS.match(user_id):
            return None
        return f"fb:{user_id}", f"https://www.facebook.com/profile.php?id={user_id}"
    if len(segments) != 1:
        return None
    name = segments[0]
    if name.lower() in _FB_NOT_PROFILES or not _FB_VANITY.match(name) or name.lower().endswith(".php"):
        return None
    if _FB_DIGITS.match(name):
        return f"fb:{name}", f"https://www.facebook.com/profile.php?id={name}"
    return f"fb:{name.lower()}", f"https://www.facebook.com/{name}"


def _instagram(href: object) -> tuple[str, str] | None:
    parts = _parts(href, IG_HOSTS)
    if parts is None:
        return None
    segments = _segments(parts.path)
    if len(segments) != 1:
        return None
    name = segments[0].lower()
    if name in _IG_NOT_PROFILES or not _IG_USERNAME.match(name) or name.strip(".") != name:
        return None
    return f"ig:{name}", f"https://www.instagram.com/{name}"


def _tiktok(href: object) -> tuple[str, str] | None:
    parts = _parts(href, TT_HOSTS)
    if parts is None:
        return None
    segments = _segments(parts.path)
    found = _TT_HANDLE.match(segments[0]) if len(segments) == 1 else None
    if found is None:
        return None
    name = found.group(1).lower()
    return f"tt:{name}", f"https://www.tiktok.com/@{name}"


def _linkedin(href: object) -> tuple[str, str] | None:
    parts = _parts(href, _LI_HOST)
    if parts is None:
        return None
    segments = _segments(parts.path)
    if len(segments) != 2 or segments[0] not in _LI_KINDS:
        return None
    kind, ident = _LI_KINDS[segments[0]], unquote(segments[1])
    if not _LI_ID.match(ident):
        return None
    ident = ident.lower()
    return f"li:{kind}:{ident}", f"https://www.linkedin.com/{kind}/{ident}"


_BY_PLATFORM = {"facebook": _facebook, "instagram": _instagram, "tiktok": _tiktok, "linkedin": _linkedin}


def profile_link(platform: str, href: object) -> tuple[str, str] | None:
    """``(key, url)`` of the person, page or company a link of ``platform`` points to, or None."""
    parse = _BY_PLATFORM.get(platform)
    return parse(href) if parse is not None else None


class Engagement(_Strict):
    """One person's comment, reaction or repost under one post, as the provider gave it.

    ``profile_key``/``profile_url`` are what :func:`profile_link` makes of the profile link (the validator checks it);
    ``text`` is the comment's own text (required for a comment, absent otherwise); ``reaction_type`` the provider's
    word for a reaction (``like``, ``love``…), None when it did not say; ``raw_id`` the provider's own id of the item.
    """

    platform: Platform
    kind: EngagementKind
    author: str | None = Field(default=None, max_length=200)
    profile_key: str = Field(max_length=140)
    profile_url: str = Field(max_length=300)
    text: str | None = Field(default=None, max_length=5000)
    reaction_type: str | None = Field(default=None, max_length=40)
    post_url: str = Field(max_length=2048)
    raw_id: str | None = Field(default=None, max_length=200)

    @field_validator("post_url")
    @classmethod
    def _http(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Engagement:
        if profile_link(self.platform, self.profile_url) != (self.profile_key, self.profile_url):
            raise ValueError("profile_key/profile_url are not a canonical profile of this platform")
        if self.kind == "comment" and self.text is None:
            raise ValueError("a comment needs its text")
        if self.kind != "comment" and self.text is not None:
            raise ValueError("only a comment has text")
        if self.kind != "reaction" and self.reaction_type is not None:
            raise ValueError("only a reaction has a reaction_type")
        return self


__all__ = ["FB_HOSTS", "PLATFORMS", "Engagement", "EngagementKind", "profile_link"]
