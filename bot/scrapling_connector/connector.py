"""One explicit URL over bounded HTTP, parsed with Scrapling's Selector.

This deliberately does not import ``scrapling.fetchers``.  The connector owns
the network policy through httpx, while Scrapling is used only to parse the
already size-bounded HTML response.  No browser, target discovery, or crawling
is available here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from scrapling import Selector

from bot.acquisition.models import NormalizedPage

from .models import ScraplingOutcome, ScraplingResult, ScraplingTask

_HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_FORBIDDEN_HOSTS = frozenset({"localhost", "metadata.google.internal"})
Resolver = Callable[[str], Awaitable[list[str]]]


class PolicyViolation(ValueError):
    """A target is outside this connector's read-only network policy."""


class FetchFailure(RuntimeError):
    """A safe, user-reportable page failure."""


async def resolve_public_addresses(host: str) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({item[4][0] for item in records})


class ScraplingConnector:
    """Fetch exactly one already-approved public website page."""

    def __init__(
        self,
        *,
        request_timeout_seconds: int = 20,
        max_content_bytes: int = 1_500_000,
        max_content_chars: int = 120_000,
        user_agent: str = "RealEstateResearchBot/0.1",
        resolver: Resolver = resolve_public_addresses,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not 3 <= request_timeout_seconds <= 60 or not 10_000 <= max_content_bytes <= 5_000_000 or not 1_000 <= max_content_chars <= 200_000:
            raise ValueError("unsafe Scrapling connector limits")
        self.request_timeout_seconds = request_timeout_seconds
        self.max_content_bytes = max_content_bytes
        self.max_content_chars = max_content_chars
        self._resolver = resolver
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout_seconds), follow_redirects=False, trust_env=False,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, task: ScraplingTask) -> ScraplingResult:
        if task.max_pages != 1:
            return ScraplingResult(task.run_id, ScraplingOutcome.FAILED, reason="invalid_page_limit")
        if not 1 <= task.max_runtime_seconds <= 300:
            return ScraplingResult(task.run_id, ScraplingOutcome.FAILED, reason="invalid_runtime_limit")
        try:
            async with asyncio.timeout(task.max_runtime_seconds):
                final_url, body = await self._download(task.target)
                return ScraplingResult(task.run_id, ScraplingOutcome.COMPLETED, self._parse(final_url, body))
        except TimeoutError:
            return ScraplingResult(task.run_id, ScraplingOutcome.STOPPED_LIMIT, reason="execution_timeout")
        except (PolicyViolation, FetchFailure) as exc:
            return ScraplingResult(task.run_id, ScraplingOutcome.FAILED, reason=str(exc))
        except httpx.TimeoutException:
            return ScraplingResult(task.run_id, ScraplingOutcome.STOPPED_LIMIT, reason="request_timeout")
        except httpx.HTTPError as exc:
            return ScraplingResult(task.run_id, ScraplingOutcome.FAILED, reason=f"request_failed:{type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - collector boundary returns structured failures.
            return ScraplingResult(task.run_id, ScraplingOutcome.FAILED, reason=f"unexpected:{type(exc).__name__}")

    async def _download(self, target: str) -> tuple[str, bytes]:
        current = target
        for redirect_count in range(4):
            await self._validate_public_https(current)
            async with self._client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchFailure("redirect_missing_location")
                    if redirect_count == 3:
                        raise FetchFailure("too_many_redirects")
                    current = urljoin(str(response.url), location)
                    continue
                if response.status_code >= 400:
                    raise FetchFailure(f"http_{response.status_code}")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and not content_type.startswith(_HTML_TYPES):
                    raise FetchFailure("unsupported_content_type")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_content_bytes:
                    raise FetchFailure("content_size_limit")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > self.max_content_bytes:
                        raise FetchFailure("content_size_limit")
                    chunks.append(chunk)
                return str(response.url), b"".join(chunks)
        raise FetchFailure("too_many_redirects")

    async def _validate_public_https(self, raw_url: str) -> None:
        parsed = urlsplit(raw_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise PolicyViolation("target_must_be_plain_https")
        host = parsed.hostname.lower().rstrip(".")
        if host in _FORBIDDEN_HOSTS:
            raise PolicyViolation("private_target_forbidden")
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addresses = await self._resolver(host)
            except OSError as exc:
                raise FetchFailure("dns_resolution_failed") from exc
        if not addresses or any(not _is_public(address) for address in addresses):
            raise PolicyViolation("private_target_forbidden")

    def _parse(self, canonical_url: str, body: bytes) -> NormalizedPage:
        selector = Selector(content=body, url=canonical_url)
        title = (selector.css("title::text").get() or "").strip()[:500]
        text_nodes = selector.xpath(
            "//body//text()[normalize-space() and not(ancestor::script or ancestor::style or ancestor::noscript or ancestor::svg or ancestor::iframe)]"
        ).getall()
        text = " ".join(part.strip() for part in text_nodes if part.strip())
        text = " ".join(text.split())[: self.max_content_chars]
        if not text:
            raise FetchFailure("no_readable_text")
        return NormalizedPage(canonical_url=canonical_url, title=title, text=text, platform="website")


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)
