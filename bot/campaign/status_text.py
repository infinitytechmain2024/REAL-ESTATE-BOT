"""The only status lines a normal user (anyone who is not an owner) ever sees.

The runner, discovery and Orchestra speak in technical stages (windows of
groups, batches, queue ids, verification). Owners keep those lines; everyone
else gets one short line that says only where the bot searches right now, with
the place as a link (HTML, escaped here): «🔎 Сейчас ищу на сайте <a>host</a>»,
«… в Facebook в группе <a>name</a>», «… в TikTok: <a>query</a>», or one of the
fixed labels below. Pure functions, no I/O, so they are easy to test.
"""

from __future__ import annotations

import html
import re
from typing import Literal
from urllib.parse import quote_plus, urlsplit

ACCEPTED = "Принято. Начинаю поиск."
SEARCHING = "Ищу…"
FACEBOOK = "Ищу в Facebook…"
TIKTOK = "Ищу в TikTok…"
INSTAGRAM = "Ищу в Instagram…"
LINKEDIN = "Ищу в LinkedIn…"
SOCIAL: dict[str, str] = {"tiktok": TIKTOK, "instagram": INSTAGRAM, "linkedin": LINKEDIN}
CHECKING = "🔎 Проверяю найденное"
DONE = "Поиск завершён."
NOTHING = "Пока ничего подходящего не нашёл."

# A search ended early because Facebook's daily limit (or its safety breaker) stopped new group reads.
LIMIT_NOTE = "Лимит Facebook на сегодня исчерпан, часть групп не проверена. Запустите поиск завтра, чтобы проверить остальные."
LIMIT_REASONS = frozenset({"facebook_daily_limit", "facebook_breaker"})

FIXED_STATUSES: frozenset[str] = frozenset({ACCEPTED, SEARCHING, FACEBOOK, TIKTOK, INSTAGRAM, LINKEDIN,
                                            CHECKING, DONE, NOTHING, f"{DONE}\n{LIMIT_NOTE}",
                                            f"{NOTHING}\n{LIMIT_NOTE}"})
_SITE_NAME = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9.\-]{0,79}$")  # a host, no spaces
MAX_GROUP_CHARS = 60
MAX_LINK_TEXT_CHARS = 60
MAX_URL_CHARS = 500
_NOW = "🔎 Сейчас ищу "
LIVE_PREFIX = _NOW
SOCIAL_NAMES = {"tiktok": "TikTok", "instagram": "Instagram", "linkedin": "LinkedIn"}
# Where each investor-reach platform lives (the reach reads search results; the status shows the platform).
PLATFORM_HOSTS = {"linkedin": "linkedin.com", "reddit": "reddit.com", "x": "x.com", "instagram": "instagram.com",
                  "tiktok": "tiktok.com", "youtube": "youtube.com", "telegram": "t.me"}
SOCIAL_SEARCH_URLS = {"tiktok": "https://www.tiktok.com/search?q=",
                      "instagram": "https://www.instagram.com/explore/search/keyword/?q=",
                      "linkedin": "https://www.linkedin.com/search/results/content/?keywords="}
_URLISH = re.compile(r"https?://|www\.", re.IGNORECASE)

Stage = Literal[
    "accepted", "planning", "discovery", "facebook", "web", "site", "social", "checking",
    "verification", "waiting", "error", "finished",
]

_ACTIVE_STATES = {"planned": "discovery", "discovering": "discovery",
                  "running": "facebook", "paused_verification": "verification"}
_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})


LAYER_NAMES = {"http": "напрямую", "browser": "браузер", "api": "API"}  # owners' technical lines


def safe_url(url: str | None) -> str | None:
    """``url`` when it is a plain http(s) address a link may point to (no spaces or control characters), else None."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_CHARS or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and host else None


def host_name(value: str | None) -> str | None:
    """A plain host (``idealista.com``, no ``www.``) from a host or a URL; None for anything else."""
    value = " ".join((value or "").split())
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0].removeprefix("www.").lower()
    return value if _SITE_NAME.match(value) else None


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def link(url: str | None, text: str) -> str:
    """``<a href="url">text</a>``, both escaped; just the escaped text when ``url`` is not a safe http(s) address."""
    shown = html.escape(text, quote=False)
    safe = safe_url(url)
    return f'<a href="{html.escape(safe, quote=True)}">{shown}</a>' if safe else shown


def site_line(host: str | None, url: str | None = None) -> str | None:
    """«🔎 Сейчас ищу на сайте <a href="URL">host</a>»: ``url`` is the page read now (or a portal search page);
    without one the site's root. None when no plain site name is known."""
    name = host_name(host) or host_name(url if safe_url(url) else None)
    if name is None:
        return None
    page = url if safe_url(url) and (host_name(url) or "").removeprefix("www.").endswith(name) else f"https://{name}/"
    return f"{_NOW}на сайте {link(page, name)}"


def group_name(name: str | None) -> str | None:
    """A readable Facebook group name (trimmed to 60 characters), or None for an id, a link or nothing."""
    name = _clip(name or "", 10_000)
    if not name or _URLISH.search(name) or not re.search(r"[^\W\d_]", name):
        return None
    return _clip(name, MAX_GROUP_CHARS)


def group_line(name: str | None, url: str | None = None) -> str:
    """«🔎 Сейчас ищу в Facebook в группе <a href="GROUP_URL">name</a>»; without a readable name the word
    «группе» is the link. Without a safe group address there is no link at all."""
    shown = group_name(name)
    if shown is None:
        return f"{_NOW}в Facebook в {link(url, 'группе')}"
    return f"{_NOW}в Facebook в группе {link(url, shown)}"


def social_line(platform: str | None, query: str | None = None, url: str | None = None) -> str | None:
    """«🔎 Сейчас ищу в TikTok: <a href="URL">запрос</a>» (Instagram, LinkedIn alike); None for another platform.

    ``url``: the search page open now (the platform's search for the query when unknown). Without a query the
    platform's name is the link and there is no colon.
    """
    name = SOCIAL_NAMES.get(platform or "")
    if name is None:
        return None
    text = _clip(query or "", MAX_LINK_TEXT_CHARS)
    key = platform or ""
    if not text:
        return f"{_NOW}в {link(url or f'https://www.{PLATFORM_HOSTS[key]}/', name)}"
    return f"{_NOW}в {name}: {link(url or SOCIAL_SEARCH_URLS[key] + quote_plus(query or ''), text)}"


def reach_line(platform: str | None, url: str | None = None) -> str | None:
    """«🔎 Сейчас ищу на <a href="URL">linkedin.com</a>» for the investor reach (search results of a platform);
    None for the open web or an unknown platform. ``url``: a result of that platform, else its own address."""
    host = PLATFORM_HOSTS.get(platform or "")
    if host is None:
        return None
    page = url if safe_url(url) and (host_name(url) or "").endswith(host) else f"https://{host}/"
    return f"{_NOW}на {link(page, host)}"


def is_live_line(text: str) -> bool:
    """True for a «🔎 Сейчас ищу …» line, alone or inside an owner's message."""
    return any(line.startswith(LIVE_PREFIX) for line in text.split("\n"))


def user_status(stage: Stage, *, site: str | None = None, found: int = 0, facebook_started: bool = False,
                platform: str | None = None) -> str:
    """Map an internal stage to the user-safe label (``site``: the site's name linked to its root, ``social``:
    the platform linked to its address).

    ``verification``/``waiting``/``error`` never say why: they keep the nearest
    working label (Facebook once the Facebook phase started, otherwise «Ищу…»).
    """
    if stage == "accepted":
        return ACCEPTED
    if stage in ("planning", "discovery"):
        return SEARCHING
    if stage == "facebook":
        return FACEBOOK
    if stage in ("web", "site"):
        return site_line(site) or SEARCHING
    if stage == "social":
        return social_line(platform) or SEARCHING
    if stage == "checking":
        return CHECKING
    if stage == "finished":
        return DONE if found > 0 else NOTHING
    return FACEBOOK if facebook_started else SEARCHING


def campaign_label(state: str, *, found: int = 0, checking: bool = False, social: str | None = None,
                   reason: str | None = None) -> str:
    """The user-safe label for a campaign in ``state`` (``found``: findings already sent;
    ``social``: the network searched right now while Facebook is idle; ``reason``: the stop reason,
    which adds the Facebook-limit note to a finished search)."""
    if state in _TERMINAL_STATES:
        finished = user_status("finished", found=found)
        return f"{finished}\n{LIMIT_NOTE}" if reason in LIMIT_REASONS else finished
    if checking:
        return CHECKING
    if social in SOCIAL:
        return user_status("social", platform=social)
    stage = _ACTIVE_STATES.get(state, "waiting")
    return user_status(stage, facebook_started=state in ("running", "paused_verification"))  # type: ignore[arg-type]


_LINK = r'<a href="[^"<>\s]+">[^<>]{1,200}</a>'
_LIVE = re.compile(
    rf"^{_NOW}(?:на сайте {_LINK}|в Facebook в (?:группе )?{_LINK}|в {_LINK}|в (?:TikTok|Instagram|LinkedIn): {_LINK}"
    rf"|на {_LINK})$")


def is_user_status(text: str) -> bool:
    """True for exactly the strings a normal user may see as a status."""
    return text in FIXED_STATUSES or _LIVE.match(text) is not None
