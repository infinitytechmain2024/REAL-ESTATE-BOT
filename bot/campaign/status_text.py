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

_PROGRESS_PREFIX = "Сейчас: сайты"
_DONE_PREFIX = "Сайты: готово"
LAYER_NAMES = {"http": "напрямую", "browser": "браузер", "api": "API"}
_PROGRESS = re.compile(
    rf"^{_PROGRESS_PREFIX}(?: · (?P<host>[A-Za-z0-9][A-Za-z0-9.\-]{{0,39}})(?: \((?P<layer>[^()]+)\))?)?"
    r" · прочитано (?P<read>\d+) · найдено (?P<found>\d+)(?: · порталов (?P<done>\d+)/(?P<total>\d+))?$")
_DONE = re.compile(rf"^{_DONE_PREFIX} · прочитано (\d+) · найдено (\d+)$")

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


def web_progress_status(host: str | None, layer: str | None, read: int, found: int, done: int = 0,
                        total: int = 0) -> str:
    """«Сейчас: сайты · idealista.com (браузер) · прочитано 37 · найдено 12 · порталов 4/8».

    The host is shown only when it is a plain site name (like ``site_status``), the layer only with a host,
    and the portals part only when some site is known.
    """
    shown = site_status(host)
    parts = [_PROGRESS_PREFIX]
    if host and shown != WEB:
        name = shown[len(_SITE_PREFIX):-len(_SITE_SUFFIX)]
        label = LAYER_NAMES.get(layer or "")
        parts.append(f"{name} ({label})" if label else name)
    parts += [f"прочитано {max(read, 0)}", f"найдено {max(found, 0)}"]
    if total > 0:
        parts.append(f"порталов {min(max(done, 0), total)}/{total}")
    return " · ".join(parts)


def web_done_status(read: int, found: int) -> str:
    """«Сайты: готово · прочитано N · найдено M» once the web stage has ended."""
    return f"{_DONE_PREFIX} · прочитано {max(read, 0)} · найдено {max(found, 0)}"


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
    if text.startswith(_DONE_PREFIX):
        return _DONE.match(text) is not None
    if text.startswith(_PROGRESS_PREFIX):
        m = _PROGRESS.match(text)
        if m is None or (m["layer"] is not None and m["layer"] not in LAYER_NAMES.values()):
            return False
        layer = next((k for k, v in LAYER_NAMES.items() if v == m["layer"]), None)
        return web_progress_status(m["host"], layer, int(m["read"]), int(m["found"]), int(m["done"] or 0),
                                   int(m["total"] or 0)) == text
    if text.startswith(_GROUP_PREFIX) and text.endswith(_GROUP_SUFFIX):
        return group_status(text[len(_GROUP_PREFIX):-len(_GROUP_SUFFIX)]) == text
    return False
