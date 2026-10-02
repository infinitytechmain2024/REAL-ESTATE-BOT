"""X (Twitter) search through ``twitter-cli`` -- Agent Reach's X backend -- for the reach.

The reach's X queries (``site:x.com …``) go to X itself instead of the search
engines: ``twitter search "<query>" -t latest -n 20 --json`` returns real, recent
posts with their author, so a post such as «busco piso en Madrid» or «invierto en
inmuebles en Marbella» is judged from its full text. Search on X needs a signed-in
session (cookies ``auth_token`` and ``ct0``): either the X profile an owner signed
in through the live window (``/login x``; read from the browser service), or
``TWITTER_AUTH_TOKEN`` / ``TWITTER_CT0`` in ``.env`` (exported with Cookie-Editor).
Without a session, or when X refuses it, the reach falls back to the search
engines and owners are asked to log in. The CLI runs with a fixed argument list
(no shell), a timeout and an output cap; only ``search`` is ever invoked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from bot.web_search.searxng import SearchError, SearchHit

if TYPE_CHECKING:
    import asyncpg

log = logging.getLogger(__name__)

MAX_OUTPUT = 4_000_000
_SITE = re.compile(r"\bsite:(?:www\.)?(?:x|twitter)\.com\S*", re.I)
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_LOGIN_CODES = ("not_authenticated", "unauthorized", "auth", "forbidden", "login")


class XUnavailable(Exception):
    """No usable X session: the reach falls back to the search engines and asks for a login."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class XSession(Protocol):
    async def get(self) -> tuple[str, str] | None: ...
    def invalidate(self) -> None: ...


class EnvSession:
    """``TWITTER_AUTH_TOKEN`` / ``TWITTER_CT0`` from the environment (a Cookie-Editor export)."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self._value = (env.get("TWITTER_AUTH_TOKEN", "").strip(), env.get("TWITTER_CT0", "").strip())

    async def get(self) -> tuple[str, str] | None:
        return self._value if all(self._value) else None

    def invalidate(self) -> None:
        return None  # a refused .env session stays refused until the owner replaces it


class BrowserSession:
    """The X profile an owner signed in through the live window, read from the browser service.

    The two cookies are kept in memory for ``cache_seconds`` and dropped when X refuses them."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], browser: Any, *, cache_seconds: float = 6 * 3600,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.pool, self.browser, self.cache_seconds, self.clock = pool, browser, cache_seconds, clock
        self._value: tuple[str, str] | None = None
        self._until = 0.0

    async def get(self) -> tuple[str, str] | None:
        if self._value is not None and self.clock() < self._until:
            return self._value
        self._value = None
        row = await self.pool.fetchrow(
            """select id::text, profile_name from browser_profiles
                where platform = 'x' and state = 'ready' and deleted_at is null
                order by last_used_at desc nulls last limit 1""")
        if row is None:
            return None
        try:
            lease = await self.browser.acquire(row["id"], row["profile_name"], "ready", platform="x")
        except Exception:  # noqa: BLE001 - the profile is busy (a live window, another lease): next time
            log.info("campaign.x_session_busy")
            return None
        try:
            found = await self.browser.x_credentials(lease)
        finally:
            await self.browser.release(lease, "READY")
        if found:
            self._value, self._until = (found["auth_token"], found["ct0"]), self.clock() + self.cache_seconds
        return self._value

    def invalidate(self) -> None:
        self._value, self._until = None, 0.0


class FirstSession:
    """The first of several sessions that has one (``.env`` first, then the live-window profile)."""

    def __init__(self, *sessions: XSession) -> None:
        self.sessions = sessions

    async def get(self) -> tuple[str, str] | None:
        for session in self.sessions:
            value = await session.get()
            if value is not None:
                return value
        return None

    def invalidate(self) -> None:
        for session in self.sessions:
            session.invalidate()


Runner = Callable[[Sequence[str], Mapping[str, str], float], Awaitable[tuple[int, bytes]]]


async def _run(args: Sequence[str], env: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL, env=dict(env))
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, out[:MAX_OUTPUT]


def x_query(text: str) -> str:
    """The query without its ``site:x.com`` part (X searches itself)."""
    return " ".join(_SITE.sub(" ", text).split())[:200]


def parse_output(raw: bytes) -> list[SearchHit]:
    """``twitter … --json``: ``{"ok": true, "data": [tweet, …]}`` -> one hit per original post."""
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise SearchError("x_not_json") from exc
    if not isinstance(payload, dict):
        raise SearchError("x_not_json")
    if payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or "error").lower()[:60]
        if any(word in code for word in _LOGIN_CODES):
            raise XUnavailable(code)
        raise SearchError(f"x_{code}")
    hits: list[SearchHit] = []
    for tweet in payload.get("data") or []:
        if not isinstance(tweet, dict) or tweet.get("isRetweet"):
            continue
        author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
        handle, tweet_id = str(author.get("screenName") or ""), str(tweet.get("id") or "")
        if not _HANDLE.match(handle) or not tweet_id.isdigit():
            continue
        name = " ".join(str(author.get("name") or "").split())[:100]
        text = " ".join(str(tweet.get("text") or "").split())[:500]
        hits.append(SearchHit(f"https://x.com/{handle}/status/{tweet_id}",
                              f"{name} (@{handle}) on X" if name else f"@{handle} on X", text))
    return hits


class TwitterCli:
    """``twitter search`` as a search engine for the reach's X queries."""

    def __init__(self, session: XSession, *, binary: str = "twitter", max_results: int = 20,
                 timeout_seconds: float = 60, proxy_url: str = "", runner: Runner | None = None) -> None:
        if not 1 <= max_results <= 50 or not 5 <= timeout_seconds <= 300:
            raise ValueError("unsafe twitter-cli limits")
        self.session, self.binary, self.max_results = session, binary, max_results
        self.timeout_seconds, self.proxy_url, self.runner = timeout_seconds, proxy_url, runner or _run

    async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]:
        text = x_query(query)
        if len(text) < 2:
            return []
        session = await self.session.get()
        if session is None:
            raise XUnavailable("no_session")
        with tempfile.TemporaryDirectory(prefix="twitter-cli-") as home:
            env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home,
                   "TWITTER_AUTH_TOKEN": session[0], "TWITTER_CT0": session[1], "NO_COLOR": "1"}
            if self.proxy_url:
                env["TWITTER_PROXY"] = self.proxy_url
            args = [self.binary, "search", text, "-t", "latest", "-n", str(self.max_results), "--json"]
            try:
                _code, out = await self.runner(args, env, self.timeout_seconds)
            except TimeoutError as exc:
                raise SearchError("x_timeout") from exc
            except OSError as exc:  # the CLI is not installed in this image
                raise SearchError("x_cli_missing") from exc
        try:
            return parse_output(out)
        except XUnavailable:
            self.session.invalidate()
            raise
