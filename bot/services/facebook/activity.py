"""Small, conservative helpers for deciding whether a Facebook group is active."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_RELATIVE = re.compile(r"^(?P<n>\d+)\s*(?P<unit>minute|minutes|min|мину|минут|hour|hours|час|часа|часов|day|days|дн|день|дня|дней)", re.I)


def is_recent(posted_at_text: str | None, *, max_age_days: int, now: datetime | None = None) -> bool:
    """Return true only for a timestamp we can positively classify as recent."""
    if not posted_at_text:
        return False
    text = " ".join(posted_at_text.lower().split())
    if any(token in text for token in ("just now", "только что", "ahora mismo")):
        return True
    match = _RELATIVE.match(text)
    if match:
        value = int(match.group("n"))
        unit = match.group("unit")
        days = value / (1440 if unit.startswith(("minute", "min", "мину", "минут")) else 24)
        if unit.startswith(("hour", "час")):
            days = value / 24
        return days <= max_age_days
    for fmt in ("%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        reference = now or datetime.now(UTC)
        return reference - parsed <= timedelta(days=max_age_days)
    # An unrecognised timestamp must never make an inactive group look active.
    return False
