"""Investor-vertical findings: one organisation on several hosts (Q60, ``INVESTOR_LEAD_SAME_ORG``) and the page of a
one-off event that already took place (Q60, ``INVESTOR_PAST_EVENT_EXCLUDED``).

``org_key``: the analysis fact ``who`` (the person, company, fund or club named by the page), normalised: accents,
case, punctuation, a parenthesised abbreviation («(PDN)») and legal suffixes («S.L.», «Ltd») dropped. ``None`` when
nothing is named or only a generic word («Private investor», «inversor particular»): never merged then.

``past_event``: the finding is the page of a single event (a ticket or event page: a ticket host such as
wegottickets.com or eventbrite, or an ``/event/`` · ``/events/`` · ``/tickets/`` · ``/entradas/`` path, or a summary
saying «ticket page» / «страница билетов») and every full date its summary names (21 August 2025, 21 de agosto de 2025,
21 августа 2025, 2025-08-21, 21.08.2025) is before ``today``. No date, a date today or later, or a club's own page:
not a past event. Sim b5-g7-s6: two wegottickets.com pages of past «Property Deal Network Marbella» meetups.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any
from urllib.parse import urlsplit

PAST_EVENT_NOTE = "прошедшее событие"


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


_LEGAL = frozenset({"s", "l", "sl", "slu", "sa", "sau", "sll", "ltd", "llc", "inc", "plc", "gmbh", "bv", "co", "the"})
# compared after ``_plain`` (accents and «й» folded like the name)
_GENERIC = frozenset(map(_plain, {
    "investor", "investors", "inversor", "inversores", "inversora", "inversionista", "inversionistas", "private",
    "privado", "privada", "particular", "particulares", "owner", "propietario", "propietaria", "agency", "agencia",
    "fund", "fondo", "company", "empresa", "developer", "promotor", "promotora", "user", "usuario", "anonymous",
    "anonimo", "unknown", "desconocido", "seller", "vendedor", "buyer", "comprador", "group", "grupo", "a", "an",
    "de", "del", "la", "el", "los", "las", "y", "and", "of", "инвестор", "частный", "частные", "инвесторы", "фонд",
    "компания", "застройщик", "агентство", "владелец", "собственник",
}))

_TICKET_HOSTS = ("wegottickets.com", "eventbrite.", "ticketmaster.", "skiddle.com", "allevents.in", "entradas.com",
                 "ticketea.", "feverup.com", "dice.fm", "lu.ma", "tickettailor.com")
_EVENT_PATH = re.compile(r"/(events?|eventos?|tickets?|entradas|whats-on)(/|$)", re.IGNORECASE)
_EVENT_WORDS = re.compile(r"\bticket page\b|\bevent page\b|\btickets? for\b|página de (?:entradas|evento)"
                          r"|страниц[аеуы] (?:билетов|события|мероприятия)", re.IGNORECASE)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
    "dec": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?([^\W\d_]+)\.?,?\s+(?:de\s+)?(\d{4})\b")
_MONTH_DAY = re.compile(r"\b([^\W\d_]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")


def org_key(payload: dict[str, Any] | None) -> str | None:
    """The normalised name of the person, company or club of an investor finding (``who``); None when not named."""
    who = (payload or {}).get("who")
    if not isinstance(who, str):
        return None
    text = re.sub(r"\([^)]*\)", " ", _plain(who))
    words = [w for w in re.split(r"[^\w]+", text) if w and w not in _LEGAL]
    if not words or all(w in _GENERIC for w in words) or len("".join(words)) < 3:
        return None
    return " ".join(words)


def same_org(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    key = org_key(a)
    return key is not None and key == org_key(b)


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def dates_named(text: str) -> list[date]:
    """Every full calendar date in ``text`` (day, month and year); a month or a year alone is no date."""
    found: list[date | None] = []
    lower = text.casefold()
    for day, month, year in _DAY_MONTH.findall(lower):
        if month in _MONTHS:
            found.append(_date(int(year), _MONTHS[month], int(day)))
    for month, day, year in _MONTH_DAY.findall(lower):
        if month in _MONTHS:
            found.append(_date(int(year), _MONTHS[month], int(day)))
    found.extend(_date(int(y), int(m), int(d)) for y, m, d in _ISO.findall(lower))
    found.extend(_date(int(y), int(m), int(d)) for d, m, y in _NUMERIC.findall(lower))
    return sorted({d for d in found if d is not None})


def event_page(url: str | None, payload: dict[str, Any] | None) -> bool:
    """The page of one event: a ticket host, an event/ticket path, or a summary that calls it a ticket page."""
    if url:
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
        if any(host == mark or host.startswith(mark) or host.endswith("." + mark) for mark in _TICKET_HOSTS):
            return True
        if _EVENT_PATH.search(parts.path or ""):
            return True
    data = payload or {}
    return any(isinstance(data.get(k), str) and _EVENT_WORDS.search(data[k]) for k in ("summary", "summary_ru"))


def past_event(url: str | None, payload: dict[str, Any] | None, today: date) -> bool:
    """A one-off event page whose every named date is before ``today`` (see the module notes)."""
    if not event_page(url, payload):
        return False
    data = payload or {}
    named = [d for k in ("summary", "summary_ru") if isinstance(data.get(k), str) for d in dates_named(data[k])]
    return bool(named) and max(named) < today
