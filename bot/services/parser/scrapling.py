"""Optional Scrapling-backed adaptive HTTP fetcher.

Scrapling is deliberately a fallback here.  The normal fetcher keeps the
project's response-size and content-type guards; this adapter is only used
when a page is blocked or has no readable text.  It never launches a browser
and never raises a page-level error into the pipeline.
"""

from __future__ import annotations

import asyncio

from bot.config import ParserSettings
from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.services.parser.extractor import extract_text

log = get_logger(__name__)


class ScraplingFetcher:
    """Use Scrapling's asynchronous fetcher while preserving our PageContent API."""

    def __init__(self, settings: ParserSettings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        try:
            from scrapling.fetchers import AsyncFetcher
        except ImportError as exc:  # pragma: no cover - exercised in deployments without extra
            raise RuntimeError("Scrapling is not installed; run pip install 'scrapling[fetchers]'") from exc
        self._fetcher = AsyncFetcher

    async def preflight(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        async def one(url: str) -> PageContent:
            async with self._semaphore:
                return await self.fetch(url)

        return {page.url: page for page in await asyncio.gather(*(one(url) for url in urls))}

    async def fetch(self, url: str) -> PageContent:
        try:
            response = await self._fetcher.get(
                url,
                headers={"User-Agent": self.settings.user_agent},
                timeout=self.settings.timeout_seconds,
            )
            body = response.body
            if isinstance(body, bytes):
                body = body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
            if len(body.encode("utf-8")) > self.settings.max_bytes:
                return PageContent(url=url, error="page exceeded the size limit", status=response.status)
            return extract_text(
                body,
                url=url,
                final_url=str(getattr(response, "url", url)),
                max_chars=self.settings.max_chars,
            ).model_copy(update={"status": response.status})
        except Exception as exc:  # noqa: BLE001 - a fallback must never abort a batch
            log.debug("parser.scrapling.failed", url=url, error=str(exc))
            return PageContent(url=url, error=f"scrapling failed: {type(exc).__name__}")

