"""Downloading the pages behind search hits.

The rules here exist because we are fetching arbitrary URLs an engine handed
us: refuse anything that does not resolve to a public address, cap the
response size (listing sites serve megabytes of markup), cap the time, refuse
non-HTML content types, and never let one bad page take down the batch -- a
failure becomes a :class:`PageContent` with ``error`` set, not an exception.

Redirects are followed by hand rather than by httpx. httpx would validate
nothing between hops, and a page that 302s to ``http://169.254.169.254/`` is
the whole SSRF attack; here every hop goes through
:func:`bot.utils.net.assert_safe_url` before it is opened.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from bot.config import ParserSettings
from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.services.parser.extractor import extract_text
from bot.utils.net import UnsafeURLError, assert_safe_url

log = get_logger(__name__)

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class PageFetcher:
    """Fetches and extracts a batch of URLs concurrently."""

    def __init__(self, settings: ParserSettings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds),
            # Redirects are followed in `_download` so that every hop is
            # screened; letting httpx do it would skip the check.
            follow_redirects=False,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en;q=0.9,*;q=0.5",
            },
        )
        self._semaphore = asyncio.Semaphore(settings.concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        """Fetch *urls* concurrently, keyed by the URL as given.

        Always returns one entry per input URL, so callers can rely on the
        mapping being complete whether or not a page loaded.
        """
        if not urls:
            return {}

        async def one(url: str) -> PageContent:
            async with self._semaphore:
                return await self.fetch(url)

        pages = await asyncio.gather(*(one(url) for url in urls))
        ok = sum(1 for page in pages if page.ok)
        log.info("parser.batch.done", requested=len(urls), extracted=ok)
        return {page.url: page for page in pages}

    async def fetch(self, url: str) -> PageContent:
        """Fetch a single URL. Never raises."""
        try:
            html, final_url = await self._download(url)
        except _FetchFailure as exc:
            log.debug("parser.fetch.failed", url=url, error=str(exc))
            return PageContent(url=url, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad page must not kill the batch
            log.warning("parser.fetch.unexpected", url=url, error=str(exc), exc_info=True)
            return PageContent(url=url, error=f"unexpected error: {type(exc).__name__}")

        return extract_text(html, url=url, final_url=final_url, max_chars=self.settings.max_chars)

    async def _download(self, url: str) -> tuple[str, str]:
        """Return ``(html, final_url)`` or raise :class:`_FetchFailure`.

        Walks the redirect chain by hand, re-screening the target of every hop.
        ``PARSER_MAX_REDIRECTS`` hops are allowed before giving up.
        """
        current = url
        for hop in range(self.settings.max_redirects + 1):
            try:
                await assert_safe_url(current)
            except UnsafeURLError as exc:
                # Logged at info rather than debug: on a redirect chain a
                # refusal is the interesting event, not routine noise.
                log.info("parser.fetch.blocked", url=current, origin=url, reason=str(exc))
                raise _FetchFailure(f"blocked: {exc}") from exc

            outcome = await self._open(current)
            if isinstance(outcome, _Body):
                return outcome.text, outcome.url

            current = urljoin(current, outcome.location)
            log.debug("parser.fetch.redirect", hop=hop + 1, to=current)

        raise _FetchFailure(f"too many redirects (over {self.settings.max_redirects})")

    async def _open(self, url: str) -> _Body | _Redirect:
        """Open *url* and return either its body or where it points next.

        The result is returned rather than stashed on ``self`` because
        :meth:`fetch_many` runs several of these concurrently on one instance.
        """
        try:
            # Streamed so an oversized body is abandoned mid-flight rather than
            # buffered in full and then discarded.
            async with self._client.stream("GET", url) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise _FetchFailure(
                            f"HTTP {response.status_code} without a Location header"
                        )
                    return _Redirect(location)

                if response.status_code >= 400:
                    raise _FetchFailure(f"HTTP {response.status_code}")

                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if content_type and not content_type.startswith(_HTML_CONTENT_TYPES):
                    raise _FetchFailure(f"unsupported content type {content_type!r}")

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.settings.max_bytes:
                    raise _FetchFailure(f"page is {int(declared) // 1024} KB, over the limit")

                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.settings.max_bytes:
                        raise _FetchFailure("page exceeded the size limit while downloading")
                    chunks.append(chunk)

                encoding = response.encoding or "utf-8"
                body = b"".join(chunks).decode(encoding, errors="replace")
                return _Body(text=body, url=str(response.url))
        except httpx.TimeoutException as exc:
            raise _FetchFailure(f"timed out after {self.settings.timeout_seconds:g}s") from exc
        except httpx.HTTPError as exc:
            raise _FetchFailure(f"request failed: {type(exc).__name__}") from exc


@dataclass(frozen=True, slots=True)
class _Body:
    """A page that was actually read."""

    text: str
    url: str


@dataclass(frozen=True, slots=True)
class _Redirect:
    """A hop to re-screen and follow."""

    location: str


class _FetchFailure(Exception):
    """Internal: a page-level failure, converted to ``PageContent.error``."""
