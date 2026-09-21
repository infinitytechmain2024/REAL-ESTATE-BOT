#!/usr/bin/env python3
"""Acceptance check for the Facebook source's wiring into the pipeline.

The reason this source can be merged before its selectors have ever run
against a real group is a single promise: however badly it misbehaves, the
search bot keeps working. It reads Facebook's markup, which is undocumented
and changes without notice, so "it broke" is the expected case, not the
exceptional one -- and the expected case must cost a user nothing but the
group results they were never getting before.

This pins that promise. A source that raises, and a source that hangs, both
have to come back as an ordinary search answered from the web alone. It also
pins the safety default (FACEBOOK_SEARCH_ENABLED off reads nothing at all)
and the merge's tie-breaking, where the Facebook copy has to win because it
carries post text no HTTP fetcher can reach.

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

from bot.config import FacebookSettings, Settings  # noqa: E402
from bot.models.result import SearchHit  # noqa: E402
from bot.services.facebook.client import FacebookSource  # noqa: E402
from bot.services.pipeline import ResearchPipeline, _merge_sources  # noqa: E402

_results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail else ''}")


def _pipeline(facebook: object | None = None) -> ResearchPipeline:
    """A pipeline with only the parts this probe exercises; the rest stays None."""
    return ResearchPipeline(
        settings=Settings(),
        llm=None,  # type: ignore[arg-type]
        search=None,  # type: ignore[arg-type]
        query_builder=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        facebook=facebook,  # type: ignore[arg-type]
    )


class _Raising:
    """What a selector change looks like from here."""

    async def search(self, parsed: object) -> list[SearchHit]:
        raise RuntimeError("selectors no longer match anything")


class _Hanging:
    """What a wedged browser or a half-loaded group looks like from here."""

    async def search(self, parsed: object) -> list[SearchHit]:
        await asyncio.sleep(3600)
        return []


class _Working:
    async def search(self, parsed: object) -> list[SearchHit]:
        return [SearchHit(url="https://www.facebook.com/groups/1/posts/9", content="post text")]


class _Untouchable:
    """A session that fails the probe if the source goes near it."""

    @property
    def lock(self) -> None:
        raise AssertionError("the browser was touched while group search is switched off")


async def main() -> int:
    # The default every existing caller gets: no source, no change in behaviour.
    check("pipeline built without the argument has no source", _pipeline().facebook is None)
    check("no source reads nothing", await _pipeline()._facebook_hits(None) == [])

    # The promise.
    hits = await _pipeline(_Raising())._facebook_hits(None)
    check("a source that raises costs the search nothing", hits == [], repr(hits))

    probe = _pipeline(_Hanging())
    probe.settings.facebook.search_timeout_seconds = 0.2
    started = time.monotonic()
    hits = await probe._facebook_hits(None)
    elapsed = time.monotonic() - started
    check(
        "a source that hangs is abandoned, not waited on",
        hits == [] and elapsed < 2,
        f"gave up after {elapsed:.2f}s",
    )

    hits = await _pipeline(_Working())._facebook_hits(None)
    check(
        "a working source reaches the pipeline with its text",
        len(hits) == 1 and hits[0].content == "post text",
    )

    # The merge. Same page, two sources: url_hash normalises the tracking
    # parameter away, and the copy that carries the text has to survive.
    facebook_hits = [SearchHit(url="https://example.com/a?utm_source=fb", content="full text")]
    web_hits = [
        SearchHit(url="https://example.com/a", snippet="a snippet"),
        SearchHit(url="https://example.com/b"),
    ]
    merged = _merge_sources(facebook_hits, web_hits)
    check("the same page from both sources appears once", len(merged) == 2, f"{len(merged)} hits")
    check("the copy carrying post text wins the tie", merged[0].content == "full text")
    check("a web-only hit is kept", merged[1].url.endswith("/b"))

    # The safety default: a session configured but group search off.
    settings = FacebookSettings(
        enabled=True,
        search_enabled=False,
        group_urls=["https://www.facebook.com/groups/1"],
    )
    out = await FacebookSource(settings, _Untouchable()).search(None)  # type: ignore[arg-type]
    check("group search off means the browser is never opened", out == [])

    print(f"\n{sum(_results)}/{len(_results)} passed")
    return 0 if all(_results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
