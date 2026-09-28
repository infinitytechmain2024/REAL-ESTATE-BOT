"""The only status lines a normal user (anyone who is not an owner) ever sees.

The runner, discovery and Orchestra speak in technical stages (windows of
groups, batches, queue ids, verification). Owners keep those lines; everyone
else gets one of the short, fixed labels below, chosen by :func:`user_status`
or :func:`campaign_label`. Pure functions, no I/O, so they are easy to test.
"""

from __future__ import annotations

import re
from typing import Literal

ACCEPTED = "Принято. Начинаю поиск."
SEARCHING = "Ищу…"
FACEBOOK = "Ищу в Facebook…"
WEB = "Ищу в интернете…"
TIKTOK = "Ищу в TikTok…"
INSTAGRAM = "Ищу в Instagram…"
LINKEDIN = "Ищу в LinkedIn…"
SOCIAL: dict[str, str] = {"tiktok": TIKTOK, "instagram": INSTAGRAM, "linkedin": LINKEDIN}
CHECKING = "Нашёл вариант, проверяю…"
DONE = "Поиск завершён."
NOTHING = "Пока ничего подходящего не нашёл."

# A search ended early because Facebook's daily limit (or its safety breaker) stopped new group reads.
LIMIT_NOTE = "Лимит Facebook на сегодня исчерпан, часть групп не проверена. Запустите поиск завтра, чтобы проверить остальные."
LIMIT_REASONS = frozenset({"facebook_daily_limit", "facebook_breaker"})

FIXED_STATUSES: frozenset[str] = frozenset({ACCEPTED, SEARCHING, FACEBOOK, WEB, TIKTOK, INSTAGRAM, LINKEDIN,
                                            CHECKING, DONE, NOTHING, f"{DONE}\n{LIMIT_NOTE}",
                                            f"{NOTHING}\n{LIMIT_NOTE}"})
_SITE_PREFIX, _SITE_SUFFIX = "Ищу на сайте ", "…"
_SITE_NAME = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9.\-]{0,39}$")  # a host or brand, no spaces
_GROUP_PREFIX, _GROUP_SUFFIX = "Ищу в группе Facebook «", "»…"
MAX_GROUP_CHARS = 60
_GROUP_UNSAFE = re.compile(r"[«»<>\"`\n\r\t]|https?://|www\.|/", re.IGNORECASE)

Stage = Literal[
    "accepted", "planning", "discovery", "facebook", "web", "site", "social", "checking",
    "verification", "waiting", "error", "finished",
]

_ACTIVE_STATES = {"planned": "discovery", "discovering": "discovery",
                  "running": "facebook", "paused_verification": "verification"}
_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})


def site_status(name: str | None) -> str:
    """«Ищу на сайте <имя>…» for a plain site name (``idealista.com``); anything else is «Ищу в интернете…»."""
    name = " ".join((name or "").split())
    if name.lower().startswith(("http://", "https://")):
        name = name.split("://", 1)[1]
    name = name.split("/", 1)[0].removeprefix("www.")
    return f"{_SITE_PREFIX}{name}{_SITE_SUFFIX}" if name and _SITE_NAME.match(name) else WEB


def group_status(name: str | None) -> str:
    """«Ищу в группе Facebook «<название>»…» for a readable group name; an id, a link or junk is «Ищу в Facebook…»."""
    name = " ".join((name or "").split())
    if len(name) > MAX_GROUP_CHARS:
        name = name[:MAX_GROUP_CHARS - 1].rstrip() + "…"
    if not name or _GROUP_UNSAFE.search(name) or not re.search(r"[^\W\d_]", name):
        return FACEBOOK
    return f"{_GROUP_PREFIX}{name}{_GROUP_SUFFIX}"


def user_status(stage: Stage, *, site: str | None = None, found: int = 0, facebook_started: bool = False,
                platform: str | None = None) -> str:
    """Map an internal stage to the user-safe label (``social``: «Ищу в TikTok…» for ``platform``).

    ``verification``/``waiting``/``error`` never say why: they keep the nearest
    working label (Facebook once the Facebook phase started, otherwise «Ищу…»).
    """
    if stage == "accepted":
        return ACCEPTED
    if stage in ("planning", "discovery"):
        return SEARCHING
    if stage == "facebook":
        return FACEBOOK
    if stage == "web":
        return WEB
    if stage == "site":
        return site_status(site)
    if stage == "social":
        return SOCIAL.get(platform or "", SEARCHING)
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


def is_user_status(text: str) -> bool:
    """True for exactly the strings a normal user may see as a status."""
    if text in FIXED_STATUSES:
        return True
    if text.startswith(_SITE_PREFIX) and text.endswith(_SITE_SUFFIX):
        return site_status(text[len(_SITE_PREFIX):-len(_SITE_SUFFIX)]) == text
    if text.startswith(_GROUP_PREFIX) and text.endswith(_GROUP_SUFFIX):
        return group_status(text[len(_GROUP_PREFIX):-len(_GROUP_SUFFIX)]) == text
    return False
