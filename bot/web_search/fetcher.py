"""Polite, bounded GETs of public pages: robots.txt, one request per host at a time, no cookies.

* Only plain ``http``/``https`` URLs whose host resolves to public addresses
  (re-checked on every redirect, at most 4); no credentials in URLs, no forms,
  no logins -- only GET.
* ``robots.txt`` is read once per host (cached) and obeyed for our user agent,
  including its ``Crawl-delay`` (capped at 30 s).
* At least ``host_interval_seconds`` between two requests to one host.
* Hard request timeout, HTML only, a byte cap, cookies dropped after each request.
* ``proxy_url`` (``WEB_SEARCH_PROXY_URL``, http:// or socks5://; a comma-separated
  list is allowed) routes every request through an outbound proxy/VPN when set.
  A host is pinned to one proxy (crc32(host) % n) so a site sees one IP; the next
  one is tried only when the connection fails. Proxy values are never logged.
* ``impersonate`` (``WEB_SEARCH_IMPERSONATE``: off|chrome|safari|firefox or a
  curl_cffi profile such as chrome124) sends requests with a real browser's TLS/HTTP2
  fingerprint and headers via curl_cffi; ``off`` or a missing curl_cffi falls back to
  httpx. robots.txt is always matched against the declared bot ``user_agent``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)
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
    scrape_credits: int | None = None  # provider-reported cost, when the unlocker supplies it


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


DEFAULT_ACCEPT_LANGUAGE = "es-ES,es;q=0.9,en;q=0.8,ru;q=0.6,uk;q=0.5"
ACCEPT_LANGUAGE_BY_COUNTRY = {
    "ES": "es-ES,es;q=0.9,en;q=0.8",
    "UA": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
}
BROWSER_USER_AGENTS = {
    "chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36",
    "firefox": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "safari": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.4 Safari/605.1.15",
}
_HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"


class ConnectFailed(FetchError):
    """The connection (or the proxy) could not be opened; the next proxy may be tried."""


@dataclass
class TransportResponse:
    status: int
    headers: dict[str, str]          # lower-case names
    chunks: AsyncIterator[bytes]


class Transport(Protocol):
    """One GET without redirects or cookies. Raises ``FetchError("timeout")``,
    ``ConnectFailed`` or ``FetchError("network_error:...")``."""

    def open(self, url: str, *, headers: dict[str, str], proxy: str | None) -> AbstractAsyncContextManager[TransportResponse]: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    def __init__(self, *, timeout_seconds: float = 20, client: httpx.AsyncClient | None = None) -> None:
        self._timeout = timeout_seconds
        self._injected = client
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    def _client(self, proxy: str | None) -> httpx.AsyncClient:
        if self._injected is not None:
            return self._injected
        if proxy not in self._clients:
            self._clients[proxy] = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout), follow_redirects=False,
                                                     trust_env=False, proxy=proxy)
        return self._clients[proxy]

    @asynccontextmanager
    async def open(self, url: str, *, headers: dict[str, str], proxy: str | None) -> AsyncIterator[TransportResponse]:
        client = self._client(proxy)
        try:
            async with client.stream("GET", url, headers=headers) as response:
                yield TransportResponse(response.status_code, {k.lower(): v for k, v in response.headers.items()},
                                        response.aiter_bytes())
        except httpx.TimeoutException as exc:
            raise FetchError("timeout") from exc
        except (httpx.ConnectError, httpx.ProxyError) as exc:
            raise ConnectFailed(f"network_error:{type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"network_error:{type(exc).__name__}") from exc
        finally:
            client.cookies.clear()  # never carry a session from one page to the next

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()


try:
    from curl_cffi import requests as _curl_requests
    from curl_cffi.curl import CurlError as _CurlError

    _CURL_ERRORS: tuple[type[BaseException], ...] = (_curl_requests.exceptions.RequestException, _CurlError)
except Exception:  # noqa: BLE001 -- curl_cffi is optional
    _CURL_ERRORS = ()


class CurlTransport:
    """curl_cffi with a browser TLS/HTTP2 fingerprint; a fresh session per request keeps no cookies."""

    def __init__(self, *, profile: str, timeout_seconds: float = 20) -> None:
        self.profile = profile
        self._timeout = timeout_seconds

    @asynccontextmanager
    async def open(self, url: str, *, headers: dict[str, str], proxy: str | None) -> AsyncIterator[TransportResponse]:
        from curl_cffi import requests as curl

        session = curl.AsyncSession(impersonate=self.profile, timeout=self._timeout)  # type: ignore[arg-type]
        response = None
        try:
            response = await session.get(url, headers=headers, proxy=proxy, allow_redirects=False, stream=True)
            yield TransportResponse(response.status_code, {k.lower(): v for k, v in response.headers.items()},
                                    response.aiter_content())
        except curl.exceptions.Timeout as exc:
            raise FetchError("timeout") from exc
        except (curl.exceptions.ConnectionError, curl.exceptions.ProxyError) as exc:
            raise ConnectFailed(f"network_error:{type(exc).__name__}") from exc
        except _CURL_ERRORS as exc:  # RequestException, or libcurl's own CurlError
            raise FetchError(f"network_error:{type(exc).__name__}") from exc
        finally:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                await session.close()

    async def aclose(self) -> None:
        return None


def curl_cffi_available() -> bool:
    try:
        from curl_cffi.requests import AsyncSession  # noqa: F401
    except Exception:  # noqa: BLE001 -- missing wheel or broken libcurl: fall back to httpx
        return False
    return True


_OFF = ("", "off", "none", "false", "0")
_ALIASES = frozenset({"chrome", "edge", "safari", "safari_ios", "safari_beta", "safari_ios_beta", "chrome_android",
                      "firefox", "tor"})  # curl_cffi resolves these to its newest profile of the family


def known_impersonation(profile: str) -> bool:
    """True when curl_cffi knows ``profile`` (a family alias or a ``BrowserType`` name); True when it cannot be checked."""
    try:
        from curl_cffi.requests.impersonate import BrowserType
    except Exception:  # noqa: BLE001 -- no curl_cffi: nothing to validate against, httpx is used anyway
        return True
    return profile in _ALIASES or profile in BrowserType.__members__


def checked_impersonation(value: str) -> str:
    """``value`` lower-cased; an unknown profile is a warning and ``chrome`` (``off`` and friends pass as they are)."""
    raw = value.strip().lower()
    if raw in _OFF or known_impersonation(raw):
        return raw
    log.warning("web_search.unknown_impersonate %s: using chrome", raw[:40])
    return "chrome"


def resolve_impersonation(value: str | None) -> str | None:
    """The curl_cffi profile to use, or None for plain httpx (off, or curl_cffi not importable)."""
    raw = checked_impersonation(os.environ.get("WEB_SEARCH_IMPERSONATE", "chrome") if value is None else value)
    if raw in _OFF:
        return None
    return raw if curl_cffi_available() else None


def browser_family(profile: str) -> str:
    for family in ("firefox", "safari"):
        if profile.startswith(family):
            return family
    return "chrome"


def split_proxies(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def pick_proxies(host: str, proxies: list[str]) -> list[str]:
    """Proxies in the order to try for ``host``: its sticky one first, then the others."""
    if not proxies:
        return []
    start = zlib.crc32(host.encode("utf-8")) % len(proxies)
    return proxies[start:] + proxies[:start]


class PageFetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        request_timeout_seconds: float = 20,
        max_content_bytes: int = 2_000_000,
        host_interval_seconds: float = 5.0,
        proxy_url: str | None = None,
        impersonate: str | None = None,
        browser_user_agent: str | None = None,
        accept_language: str = DEFAULT_ACCEPT_LANGUAGE,
        resolver: Resolver = resolve_public_addresses,
        client: httpx.AsyncClient | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 3 <= request_timeout_seconds <= 120 or not 10_000 <= max_content_bytes <= 10_000_000 or host_interval_seconds < 0:
            raise ValueError("unsafe web fetcher limits")
        self.user_agent = user_agent  # declared bot identity: robots.txt is matched against it
        self.max_content_bytes = max_content_bytes
        self.host_interval_seconds = host_interval_seconds
        self.accept_language = accept_language
        self._proxies = split_proxies(proxy_url)
        self._resolver, self._sleep, self._clock = resolver, sleep, clock
        self._owns_transport = transport is None
        # An injected httpx client or transport is used as given; otherwise impersonate via curl_cffi when possible.
        self.profile: str | None = None if (client is not None or transport is not None) else resolve_impersonation(impersonate)
        if transport is not None:
            self._transport: Transport = transport
            self.profile = getattr(transport, "profile", None)
        elif self.profile:
            self._transport = CurlTransport(profile=self.profile, timeout_seconds=request_timeout_seconds)
        else:
            self._transport = HttpxTransport(timeout_seconds=request_timeout_seconds, client=client)
            self._owns_transport = client is None
        self.browser_user_agent = browser_user_agent or BROWSER_USER_AGENTS[browser_family(self.profile or "chrome")]
        self._robots: dict[str, _Robots] = {}
        self._last: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()

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

    async def fetch(self, url: str, *, country: str | None = None) -> FetchedPage:
        language = ACCEPT_LANGUAGE_BY_COUNTRY.get((country or "").upper(), self.accept_language)
        final_url, body = await self._get(url, max_bytes=self.max_content_bytes, html_only=True, language=language)
        return FetchedPage(final_url, _decode(body))

    def request_headers(self, *, language: str | None = None, html: bool = True) -> dict[str, str]:
        language = language or self.accept_language
        if not self.profile:  # plain httpx: the declared bot identity, as before
            return {"User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1", "Accept-Language": language}
        if not html:  # robots.txt and the like: no navigation headers
            return {"User-Agent": self.browser_user_agent, "Accept": "*/*", "Accept-Language": language}
        return {"User-Agent": self.browser_user_agent, "Accept": _HTML_ACCEPT, "Accept-Language": language,
                "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"}

    async def _get(self, url: str, *, max_bytes: int, html_only: bool, language: str | None = None) -> tuple[str, bytes]:
        current = url
        for hop in range(5):
            host = await self._validate(current)
            async with self._lock(host):
                await self._pace(host, current)
                try:
                    redirect = await self._once(current, host, max_bytes=max_bytes, html_only=html_only,
                                                headers=self.request_headers(language=language, html=html_only))
                finally:
                    self._last[host] = self._clock()
            if isinstance(redirect, bytes):
                return current, redirect
            if hop == 4:
                raise FetchError("too_many_redirects")
            current = urljoin(current, redirect[0])
        raise FetchError("too_many_redirects")

    async def _once(self, url: str, host: str, *, max_bytes: int, html_only: bool,
                    headers: dict[str, str]) -> bytes | tuple[str]:
        """One hop: the body, or ``(location,)`` for a redirect. Tries the next proxy only on connect failure."""
        candidates: list[str | None] = list(pick_proxies(host, self._proxies)) or [None]
        for index, proxy in enumerate(candidates):
            try:
                async with self._transport.open(url, headers=headers, proxy=proxy) as response:
                    return await self._read(response, max_bytes=max_bytes, html_only=html_only)
            except ConnectFailed:
                if index == len(candidates) - 1:
                    raise
        raise FetchError("network_error:no_route")  # unreachable

    @staticmethod
    async def _read(response: TransportResponse, *, max_bytes: int, html_only: bool) -> bytes | tuple[str]:
        if response.status in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise FetchError("redirect_missing_location")
            return (location,)
        if response.status >= 400:
            raise FetchError(f"http_{response.status}")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if html_only and content_type and not content_type.startswith(_HTML_TYPES):
            raise FetchError("not_html")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise FetchError("too_large")
        chunks: list[bytes] = []
        received = 0
        async for chunk in response.chunks:
            received += len(chunk)
            if received > max_bytes:
                raise FetchError("too_large")
            chunks.append(chunk)
        return b"".join(chunks)

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
