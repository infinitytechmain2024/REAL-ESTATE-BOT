"""Telegram Mini App identity: the user id Telegram itself signed.

Shared by the Telegram control plane's /login window and the verification
page. https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# initData is minted when the Mini App opens; older data is a replay.
INIT_DATA_MAX_AGE_SECONDS = 600
TELEGRAM_WEB_APP_JS = "https://telegram.org/js/telegram-web-app.js"


def verify_init_data(init_data: str, bot_token: str, *, now: float | None = None, max_age: int = INIT_DATA_MAX_AGE_SECONDS) -> int | None:
    """Return the Telegram user id signed into Mini App ``initData``, else ``None``."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user_id = int(json.loads(pairs.get("user", "{}"))["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if abs((time.time() if now is None else now) - auth_date) > max_age:
        return None
    return user_id
