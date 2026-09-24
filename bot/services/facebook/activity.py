"""How old is a post, and is the group it came from worth keeping.

Facebook stamps a post with whatever its interface language renders: "2 d" and
"12 h" in English, "hace 3 días" and "Ayer" in Spanish, "2 ч." and "5 дн." in
Russian, "2 год" and "3 тиж" in Ukrainian. The account's language is not the
product's choice -- the operator logs in by hand -- so all of them have to
parse, or a readable group dates itself out of the results.

:func:`age_days` answers in three states on purpose: a number, or ``None`` for
a stamp it cannot read. Callers must not read ``None`` as "old". Being unable
to date a post is not evidence about the post, and the caller that acts on it
has already paid to read the group.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_HOUR = 1 / 24
_MINUTE = 1 / 1440

#: Unit -> days, longest spelling first so "mo" wins over "m" and "дн" over "д".
_UNITS: tuple[tuple[str, float], ...] = tuple(
    sorted(
        (
            # minutes. Facebook's compact English form for a minute is "m";
            # a month is never compact, it becomes a date.
            ("minutes", _MINUTE), ("minute", _MINUTE), ("min", _MINUTE),
            ("minutos", _MINUTE), ("minuto", _MINUTE),
            ("минут", _MINUTE), ("мин", _MINUTE), ("хвилин", _MINUTE), ("хв", _MINUTE),
            ("m", _MINUTE),
            # hours. Ukrainian "год" is a shortened "година" -- an hour --
            # while Russian writes an hour as "ч". Nothing spells a year "год"
            # here: Facebook renders anything that old as a date.
            ("hours", _HOUR), ("hour", _HOUR), ("hrs", _HOUR), ("hr", _HOUR),
            ("horas", _HOUR), ("hora", _HOUR),
            ("часов", _HOUR), ("часа", _HOUR), ("час", _HOUR), ("ч", _HOUR),
            ("годин", _HOUR), ("год", _HOUR),
            ("h", _HOUR),
            # days
            ("days", 1.0), ("day", 1.0), ("dias", 1.0), ("dia", 1.0),
            ("дней", 1.0), ("дня", 1.0), ("день", 1.0), ("дні", 1.0), ("днів", 1.0),
            ("дн", 1.0), ("д", 1.0), ("d", 1.0),
            # weeks
            ("weeks", 7.0), ("week", 7.0), ("wk", 7.0),
            ("semanas", 7.0), ("semana", 7.0), ("sem", 7.0),
            ("недель", 7.0), ("недели", 7.0), ("неделю", 7.0), ("нед", 7.0),
            ("тижнів", 7.0), ("тижні", 7.0), ("тиж", 7.0),
            ("w", 7.0),
            # months
            ("months", 30.0), ("month", 30.0), ("mo", 30.0),
            ("meses", 30.0), ("mes", 30.0),
            ("месяцев", 30.0), ("месяца", 30.0), ("месяц", 30.0), ("мес", 30.0),
            ("місяців", 30.0), ("місяці", 30.0), ("міс", 30.0),
            # years
            ("years", 365.0), ("year", 365.0), ("yr", 365.0),
            ("anos", 365.0), ("ano", 365.0),
            ("лет", 365.0), ("года", 365.0), ("роки", 365.0), ("років", 365.0),
            ("рік", 365.0), ("y", 365.0),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

_NOW = ("just now", "только что", "щойно", "ahora mismo", "ahora")
_YESTERDAY = ("yesterday", "вчера", "вчора", "ayer")
_RELATIVE = re.compile(r"(?:hace\s+)?(?P<n>\d+)\s*(?P<unit>[^\W\d_]+)", re.UNICODE)
_ABSOLUTE = ("%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d.%m.%Y", "%d/%m/%Y")


def age_days(posted_at_text: str | None, *, now: datetime | None = None) -> float | None:
    """Age of the post in days, or ``None`` when the stamp cannot be read."""
    if not posted_at_text:
        return None
    # Accents come off so "días" matches "dias"; the digits and the unit are
    # all that is read.
    text = " ".join(posted_at_text.lower().split())
    text = text.replace("í", "i").replace("á", "a").replace("é", "e").replace("ñ", "n")

    if any(token in text for token in _NOW):
        return 0.0
    if any(token in text for token in _YESTERDAY):
        return 1.0

    match = _RELATIVE.match(text)
    if match:
        unit = match.group("unit")
        for spelling, days in _UNITS:
            # A one-letter unit has to be exactly that letter, or the "de" of
            # "21 de septiembre" reads as 21 days -- a date silently rewritten
            # into a relative age.
            if unit == spelling or (len(spelling) > 1 and unit.startswith(spelling)):
                return int(match.group("n")) * days

    for fmt in _ABSOLUTE:
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return ((now or datetime.now(UTC)) - parsed) / timedelta(days=1)
    return None


def is_recent(posted_at_text: str | None, *, max_age_days: int, now: datetime | None = None) -> bool:
    """Whether the post can be positively dated as recent.

    Strict on purpose: this decides whether a group is recorded as active and
    whether it is worth asking to join, and neither should happen on a guess.
    """
    age = age_days(posted_at_text, now=now)
    return age is not None and age <= max_age_days


def is_stale(posted_at_text: str | None, *, max_age_days: int, now: datetime | None = None) -> bool:
    """Whether the post can be positively dated as *too old*.

    The other side of :func:`is_recent`, and not its negation: an unreadable
    stamp is neither. Used where the cost of being wrong is discarding posts
    that have already been read.
    """
    age = age_days(posted_at_text, now=now)
    return age is not None and age > max_age_days
