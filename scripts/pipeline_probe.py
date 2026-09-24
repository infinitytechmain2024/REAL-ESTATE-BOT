#!/usr/bin/env python3
"""Acceptance check for how extra hit sources are wired into the pipeline.

The Facebook group reader drives a shared browser through Facebook's markup,
which is undocumented and changes without notice, so "it broke" is the
expected case, not the exceptional one -- and the expected case must cost a
user nothing but that source's results.

This pins that promise. A source that raises, and a source that hangs, both
have to come back as an ordinary failed source, so the search is answered
from what the others found. It also pins the merge: when the same page
arrives from the web and from Facebook, the post text only Facebook could
read has to survive.

Usage:
    python scripts/pipeline_probe.py

Touches no browser and no network. Exits non-zero on the first broken
promise, so it belongs in `make check` and in CI.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env.example")

from bot.config import Settings  # noqa: E402
from bot.models.result import SearchHit  # noqa: E402
from bot.services.pipeline import (  # noqa: E402
    ResearchPipeline,
    SourceSearchResult,
    _merge_hits,
)

_results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail else ''}")


def _pipeline() -> ResearchPipeline:
    """A pipeline with only the parts this probe exercises; the rest stays None."""
    return ResearchPipeline(
        settings=Settings(),
        llm=None,  # type: ignore[arg-type]
        search=None,  # type: ignore[arg-type]
        query_builder=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
    )


async def _raising(parsed: object) -> SourceSearchResult:
    """What a selector change looks like from here."""
    raise RuntimeError("selectors no longer match anything")


async def _hanging(parsed: object) -> SourceSearchResult:
    """What a wedged browser or a half-loaded group looks like from here."""
    await asyncio.sleep(3600)
    return SourceSearchResult()


async def _working(parsed: object) -> SourceSearchResult:
    return SourceSearchResult(
        hits=[SearchHit(url="https://www.facebook.com/groups/1/posts/9", content="post text")]
    )


async def main() -> int:
    # The default every caller that passes no sources gets: nothing extra.
    check("pipeline built without sources has none", _pipeline().sources == {})

    # The promise.
    out = await _pipeline()._read_source("facebook", _raising, None)  # type: ignore[arg-type]
    check(
        "a source that raises is reported failed, not raised",
        out.failed and out.hits == [],
        repr(out),
    )

    probe = _pipeline()
    probe.settings.pipeline.source_timeout_seconds = 0.2
    started = time.monotonic()
    out = await probe._read_source("facebook", _hanging, None)  # type: ignore[arg-type]
    elapsed = time.monotonic() - started
    check(
        "a source that hangs is abandoned, not waited on",
        out.failed and out.hits == [] and elapsed < 2,
        f"gave up after {elapsed:.2f}s",
    )

    out = await _pipeline()._read_source("facebook", _working, None)  # type: ignore[arg-type]
    check(
        "a working source reaches the pipeline with its text",
        not out.failed and len(out.hits) == 1 and out.hits[0].content == "post text",
    )

    # The merge. Same page, two sources: url_hash normalises the tracking
    # parameter away, and the text only Facebook could read has to survive
    # even though the web copy arrived first.
    merged = _merge_hits(
        [
            SearchHit(url="https://example.com/a", snippet="a snippet"),
            SearchHit(url="https://example.com/b"),
            SearchHit(url="https://example.com/a?utm_source=fb", content="full text"),
        ]
    )
    check("the same page from both sources appears once", len(merged) == 2, f"{len(merged)} hits")
    check("the post text survives the merge", merged[0].content == "full text")
    check("a web-only hit is kept", merged[1].url.endswith("/b"))

    print(f"\n{sum(_results)}/{len(_results)} passed")
    return 0 if all(_results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
