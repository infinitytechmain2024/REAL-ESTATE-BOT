"""A single-slot, disk-persisted access token for the remote live-view gate.

There is exactly one Facebook session and therefore at most one login/
checkpoint incident open at a time, so this deliberately does not model many
concurrent tokens: creating a new one replaces whatever was there before,
which is also what makes an old Telegram button harmlessly stop working
the moment a new alert is sent.

"One-time use" in the spec this implements means "valid for one incident,
until the session recovers or the link expires" -- not "consumed on first
HTTP request". The admin's browser makes many requests (HTML, JS, a
long-lived WebSocket) while actually logging in, so a token must stay valid
across all of them; :meth:`TokenStore.invalidate` is what ends that, called
once the watcher observes ``SessionState.HEALTHY``.

Persisted to a small JSON file (not the database) so a bot restart mid-
incident does not lose the token and force a spurious second alert -- see
the implementation notes in the noVNC spec. A file is enough for a single
concurrent incident on a single bot process; this would need to move to a
shared store the day this process runs more than once.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from bot.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class _TokenRecord:
    token: str
    exp: float


class TokenStore:
    """Async-safe, process-local cache over a one-record JSON file."""

    def __init__(self, path: str) -> None:
        self._path = Path(path).expanduser()
        self._lock = asyncio.Lock()

    async def create(self, ttl_seconds: int) -> str:
        """Mint a new token, replacing (invalidating) any existing one."""
        async with self._lock:
            token = secrets.token_urlsafe(32)
            record = _TokenRecord(token=token, exp=time.time() + ttl_seconds)
            self._write(record)
            log.info("facebook.gate.token_created", ttl_seconds=ttl_seconds)
            return token

    async def get_or_create(self, ttl_seconds: int) -> str:
        """Reuse the current token if it is still valid, else mint a new one.

        This is what the admin handler calls on every status check and every
        "start login" tap -- calling :meth:`create` there instead would mint a
        fresh token (and thus invalidate the old one) every time the admin taps
        a button, which would kill a login the admin is *already* mid-way
        through in an open browser tab. A fresh token is only warranted once the
        old one has actually expired.
        """
        async with self._lock:
            record = self._read()
            if record is not None and time.time() <= record.exp:
                return record.token
            token = secrets.token_urlsafe(32)
            new_record = _TokenRecord(token=token, exp=time.time() + ttl_seconds)
            self._write(new_record)
            log.info("facebook.gate.token_created", ttl_seconds=ttl_seconds)
            return token

    async def validate(self, token: str) -> bool:
        async with self._lock:
            record = self._read()
            if record is None:
                return False
            if not secrets.compare_digest(record.token, token):
                return False
            return time.time() <= record.exp

    async def invalidate(self) -> None:
        async with self._lock:
            if self._path.exists():
                self._path.unlink()
            log.info("facebook.gate.token_invalidated")

    def _read(self) -> _TokenRecord | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return _TokenRecord(**data)
        except (json.JSONDecodeError, TypeError, KeyError, OSError):
            log.warning("facebook.gate.token_file_unreadable", path=str(self._path))
            return None

    def _write(self, record: _TokenRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(record)), encoding="utf-8")
        tmp.replace(self._path)
