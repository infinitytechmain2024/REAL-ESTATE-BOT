"""Small Apify helpers shared by every source (light: no ``bot.campaign`` or ``bot.facebook_collector`` import).

``PENDING``: the ``run_id`` placeholder written before a paid launch (v2 H1, ``docs/v2/HANDOFF.md`` §4 item 2): no
real id came back, so the row is never launched again. ``scrub``: an actor's raw item without any key that could
carry session data (cookies, tokens) -- never stored, never logged.
"""

from __future__ import annotations

import re
from typing import Any

PENDING = "pending:"
_SECRET_KEY = re.compile(r"cookie|session|token|password|secret|auth", re.IGNORECASE)


def scrub(value: Any) -> Any:
    """The raw item without any key that could carry session data (cookies, tokens): never stored, never logged."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


__all__ = ["PENDING", "scrub"]
