"""Polite, bounded GETs of public pages: robots.txt, one request per host at a time, no cookies.

* Only plain ``http``/``https`` URLs whose host resolves to public addresses
  (re-checked on every redirect, at most 4); no credentials in URLs, no forms,
  no logins -- only GET.
* ``robots.txt`` is read once per host (cached) and obeyed for our user agent,
  including its ``Crawl-delay`` (capped at 30 s).
* At least ``host_interval_seconds`` between two requests to one host.
* Hard request timeout, HTML only, a byte cap, cookies dropped after each request.
* ``proxy_url`` (``WEB_SEARCH_PROXY_URL``, http:// or socks5://) routes every
  request through an outbound proxy/VPN when set. It is never logged.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

Resolver = Callable[[str], Awaitable[list[str]]]
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_FORBIDDEN_HOSTS = frozenset({"localhost", "metadata.google.internal"})
ROBOTS_TTL_SECONDS = 12 * 3600
ROBOTS_RETRY_SECONDS = 600
MAX_CRAWL_DELAY = 30.0
_CHARSET = re.compile(rb"charset=[\"']?([A-Za-z0-9_-]{2,40})", re.IGNORECASE)


class FetchError(RuntimeError):
    """A page that could not be read; ``code`` is short and safe to store and log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:80]


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str       # the final URL after redirects
    html: str


async def resolve_public_addresses(host: str) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({item[4][0] for item in records})


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified)


@dataclass
class _Robots:
    parser: RobotFileParser | None  # None: every path allowed
    expires: float
    delay: float = 0.0
    denied: bool = False            # robots.txt unreachable (5xx/network): nothing allowed until expires


class PageFetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        request_timeout_seconds: float = 20,
        max_content_bytes: int = 2_000_000,
        host_interval_seconds: float = 5.0,
        proxy_url: str | None = None,
        resolver: Resolver = resolve_public_addresses,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 3 <= request_timeout_seconds <= 120 or not 10_000 <= max_content_bytes <= 10_000_000 or host_interval_seconds < 0:
            raise ValueError("unsafe web fetcher limits")
        self.user_agent = user_agent
        self.max_content_bytes = max_content_bytes
        self.host_interval_seconds = host_interval_seconds
        self._resolver, self._sleep, self._clock = resolver, sleep, clock
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout_seconds), follow_redirects=False, trust_env=False,
            proxy=proxy_url or None,
            headers={"User-Agent": user_agent,
                     "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                     "Accept-Language": "es-ES,es;q=0.9,en;q=0.8,ru;q=0.6,uk;q=0.5"},
        )
        self._robots: dict[str, _Robots] = {}
        self._last: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- robots.txt --

    async def allowed(self, url: str) -> bool:
        """Whether robots.txt lets our user agent read ``url`` (fetching robots.txt on first use)."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        entry = self._robots.get(origin)
        if entry is None or entry.expires <= self._clock():
            entry = await self._load_robots(origin)
            self._robots[origin] = entry
        if entry.denied:
            return False
        return entry.parser is None or entry.parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float:
        parts = urlsplit(url)
        entry = self._robots.get(f"{parts.scheme}://{parts.netloc}")
        return entry.delay if entry else 0.0

    async def _load_robots(self, origin: str) -> _Robots:
        now = self._clock()
        try:
            status, body = await self._get(origin + "/robots.txt", max_bytes=500_000, html_only=False)
        except FetchError as exc:
            if exc.code.startswith("http_4"):
                return _Robots(None, now + ROBOTS_TTL_SECONDS)
            if exc.code in ("private_target_forbidden", "target_must_be_http"):
                return _Robots(None, now + ROBOTS_TTL_SECONDS, denied=True)
            return _Robots(None, now + ROBOTS_RETRY_SECONDS, denied=True)
        del status
        parser = RobotFileParser()
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        delay = parser.crawl_delay(self.user_agent)
        return _Robots(parser, now + ROBOTS_TTL_SECONDS, float(min(delay or 0, MAX_CRAWL_DELAY)))

    # -- pages --

    async def fetch(self, url: str) -> FetchedPage:
        final_url, body = await self._get(url, max_bytes=self.max_content_bytes, html_only=True)
        return FetchedPage(final_url, _decode(body))

    async def _get(self, url: str, *, max_bytes: int, html_only: bool) -> tuple[str, bytes]:
        current = url
        for hop in range(5):
            host = await self._validate(current)
            async with self._lock(host):
                await self._pace(host, current)
                try:
                    async with self._client.stream("GET", current) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise FetchError("redirect_missing_location")
                            if hop == 4:
                                raise FetchError("too_many_redirects")
                            current = urljoin(str(response.url), location)
                            continue
                        if response.status_code >= 400:
                            raise FetchError(f"http_{response.status_code}")
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if html_only and content_type and not content_type.startswith(_HTML_TYPES):
                            raise FetchError("not_html")
                        declared = response.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > max_bytes:
                            raise FetchError("too_large")
                        chunks: list[bytes] = []
                        received = 0
                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > max_bytes:
                                raise FetchError("too_large")
                            chunks.append(chunk)
                        return str(response.url), b"".join(chunks)
                except httpx.TimeoutException as exc:
                    raise FetchError("timeout") from exc
                except httpx.HTTPError as exc:
                    raise FetchError(f"network_error:{type(exc).__name__}") from exc
                finally:
                    self._client.cookies.clear()  # never carry a session from one page to the next
                    self._last[host] = self._clock()
        raise FetchError("too_many_redirects")

    def _lock(self, host: str) -> asyncio.Lock:
        return self._host_locks.setdefault(host, asyncio.Lock())

    async def _pace(self, host: str, url: str) -> None:
        last = self._last.get(host)
        if last is None:
            return
        interval = max(self.host_interval_seconds, self.crawl_delay(url))
        wait = last + interval - self._clock()
        if wait > 0:
            await self._sleep(wait)

    async def _validate(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise FetchError("target_must_be_http")
        if parts.port not in (None, 80, 443):
            raise FetchError("target_must_be_http")
        host = parts.hostname.lower().rstrip(".")
        if host in _FORBIDDEN_HOSTS:
            raise FetchError("private_target_forbidden")
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addresses = await self._resolver(host)
            except OSError as exc:
                raise FetchError("dns_failed") from exc
        if not addresses or any(not _is_public(a) for a in addresses):
            raise FetchError("private_target_forbidden")
        return host


def _decode(body: bytes) -> str:
    found = _CHARSET.search(body[:4096])
    if found:
        try:
            return body.decode(found.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")
