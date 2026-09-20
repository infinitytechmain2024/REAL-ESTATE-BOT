"""Downloading the pages behind search hits.

The rules here exist because we are fetching arbitrary URLs an engine handed
us: cap the response size (listing sites serve megabytes of markup), cap the
time, refuse non-HTML content types, and never let one bad page take down the
batch -- a failure becomes a :class:`PageContent` with ``error`` set, not an
exception.
"""

from __future__ import annotations

import asyncio

import httpx

from bot.config import ParserSettings
from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.services.parser.extractor import extract_text

log = get_logger(__name__)

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class PageFetcher:
    """Fetches and extracts a batch of URLs concurrently."""

    def __init__(self, settings: ParserSettings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en;q=0.9,*;q=0.5",
            },
            # Some listing sites redirect through several hops.
            max_redirects=5,
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
            log.debug("parser.fetch.failed", url=url, error=str(exc), status=exc.status)
            return PageContent(url=url, error=str(exc), status=exc.status)
        except Exception as exc:
            log.warning("parser.fetch.unexpected", url=url, error=str(exc), exc_info=True)
            return PageContent(url=url, error=f"unexpected error: {type(exc).__name__}")

        return extract_text(html, url=url, final_url=final_url, max_chars=self.settings.max_chars)

    async def _download(self, url: str) -> tuple[str, str]:
        """Return ``(html, final_url)`` or raise :class:`_FetchFailure`."""
        try:
            # Streamed so an oversized body is abandoned mid-flight rather than
            # buffered in full and then discarded.
            async with self._client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise _FetchFailure(f"HTTP {response.status_code}", status=response.status_code)

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

                body = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                return body.decode(encoding, errors="replace"), str(response.url)
        except httpx.TimeoutException as exc:
            raise _FetchFailure(f"timed out after {self.settings.timeout_seconds:g}s") from exc
        except httpx.TooManyRedirects as exc:
            raise _FetchFailure("too many redirects") from exc
        except httpx.HTTPError as exc:
            raise _FetchFailure(f"request failed: {type(exc).__name__}") from exc


class _FetchFailure(Exception):
    """Internal: a page-level failure, converted to ``PageContent.error``.

    ``status`` is set only when the failure *was* an HTTP response, so that a
    caller can tell bot protection (403/429) from a page that simply is not
    there -- see :attr:`bot.models.result.PageContent.blocked`.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
