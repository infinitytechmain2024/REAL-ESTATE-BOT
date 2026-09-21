"""Optional Scrapling-backed adaptive HTTP fetcher.

Scrapling is deliberately a fallback here.  The normal fetcher keeps the
project's response-size and content-type guards; this adapter is only used
when a page is blocked or has no readable text.  It never launches a browser
and never raises a page-level error into the pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

from bot.config import ParserSettings
from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.services.parser.extractor import extract_text

log = get_logger(__name__)


class ScraplingFetcher:
    """Use Scrapling's asynchronous fetcher while preserving our PageContent API."""

    def __init__(self, settings: ParserSettings, fetcher: Any | None = None) -> None:
        """*fetcher* substitutes for Scrapling's ``AsyncFetcher``.

        The wrapper's own behaviour -- the size guard, the timeout, the
        extraction -- is the part worth testing, and none of it needs the
        optional dependency to be installed. Passing it in keeps those tests
        offline, which matters here: the extra cannot be installed alongside
        this project's playwright pin (see requirements.txt).
        """
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        if fetcher is None:
            try:
                from scrapling.fetchers import AsyncFetcher
            except ImportError as exc:  # pragma: no cover - the usual case here
                raise RuntimeError(
                    "Scrapling is not installed; run pip install 'scrapling[fetchers]'"
                ) from exc
            fetcher = AsyncFetcher
        self._fetcher = fetcher

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
            response = await asyncio.wait_for(
                self._fetcher.get(
                    url,
                    headers={"User-Agent": self.settings.user_agent},
                    timeout=self.settings.timeout_seconds,
                ),
                timeout=self.settings.timeout_seconds,
            )
            body = getattr(response, "body", b"")
            if body is None:
                body = b""
            if isinstance(body, bytes):
                body = body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
            elif not isinstance(body, str):
                body = str(body)
            status = getattr(response, "status", None)
            if len(body.encode("utf-8")) > self.settings.max_bytes:
                return PageContent(url=url, error="page exceeded the size limit", status=status)
            return extract_text(
                body,
                url=url,
                final_url=str(getattr(response, "url", None) or url),
                max_chars=self.settings.max_chars,
            ).model_copy(update={"status": status})
        except TimeoutError:
            error = f"scrapling timed out after {self.settings.timeout_seconds:g}s"
            log.debug("parser.scrapling.failed", url=url, error=error)
            return PageContent(url=url, error=error)
        except Exception as exc:  # noqa: BLE001 - a fallback must never abort a batch
            log.debug("parser.scrapling.failed", url=url, error=str(exc))
            return PageContent(url=url, error=f"scrapling failed: {type(exc).__name__}")
