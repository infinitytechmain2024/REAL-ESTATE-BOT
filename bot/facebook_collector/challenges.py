"""Conservative multi-signal Facebook challenge recognition."""

from __future__ import annotations

from collections.abc import Mapping

_URL_SIGNALS = ("/checkpoint", "/login", "/recover", "/security/", "/two_factor")
_TEXT_SIGNALS = (
    "captcha", "security check", "confirm it's you", "suspicious activity",
    "unusual activity", "we detected automated behavior", "account disabled",
    "account restricted", "review your account", "log in to facebook", "log into facebook",
    "introduce los caracteres", "actividad sospechosa", "confirma que eres",
)


def detect_challenge(snapshot: Mapping[str, object]) -> str | None:
    """Return a reason on a high-confidence safety signal, otherwise ``None``."""
    url = str(snapshot.get("url", "")).lower()
    text = f"{snapshot.get('title', '')}\n{snapshot.get('text', '')}".lower()
    for signal in _URL_SIGNALS:
        if signal in url:
            return f"facebook_url:{signal}"
    for signal in _TEXT_SIGNALS:
        if signal in text:
            return f"facebook_page:{signal}"
    return None

