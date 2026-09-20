#!/usr/bin/env python3
"""Stage 5 validation: can we actually read the listing portals, and is the text usable?

Two questions per site, and they are different questions:

1. **Do we get the page at all?** Large portals answer a plain HTTP client
   with 403 however polite its headers are. This runs the real routing
   fetcher, so a domain listed in PARSER_BROWSER_DOMAINS goes straight to the
   browser and anything that looks blocked is escalated to it -- exactly what
   the bot does in production.
2. **Is what came back worth ranking?** A 200 that extracts to a cookie
   banner is not a working portal. Each page is checked for the things a
   property listing must contain -- a price, an area, a location -- because
   "we fetched it" and "we can use it" are not the same claim.

Usage:
    python scripts/portal_probe.py <url> [<url> ...]

    # or take the list from PARSER_PROBE_URLS in .env (comma separated)
    python scripts/portal_probe.py

Run it from the machine that will host the bot. Egress matters: the same URL
can answer differently from a home connection and from a datacenter, which is
much of why DEPLOYMENT.md argues for the home machine.

Turning the browser on for the run:

    PARSER_BROWSER_ENABLED=true python scripts/portal_probe.py <url>

It exits non-zero if any URL came back unusable, so it can gate a deploy.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import get_settings
from bot.models.result import PageContent
from bot.services.parser import build_fetcher
from bot.utils.urls import domain_of

#: What a property listing page should mention. Deliberately loose -- this
#: judges whether extraction produced a listing at all, not whether any
#: particular field parsed correctly.
_PRICE = re.compile(r"(€|EUR|\beuros?\b)", re.IGNORECASE)
_AREA = re.compile(r"\bm\s?(²|2)\b|\bmetros\b", re.IGNORECASE)
_ROOMS = re.compile(r"\bhabitaci|\bdormitor|\bbed\s?room|\bhab\b", re.IGNORECASE)

#: Below this, whatever came back is a banner or an interstitial, not a page.
_MIN_USEFUL_CHARS = 400


@dataclass
class Verdict:
    url: str
    page: PageContent
    signals: list[str]

    @property
    def domain(self) -> str:
        return domain_of(self.url) or self.url

    @property
    def usable(self) -> bool:
        """Fetched, substantial, and looking like property content."""
        return self.page.ok and len(self.page.text) >= _MIN_USEFUL_CHARS and len(self.signals) >= 2

    @property
    def summary(self) -> str:
        if self.usable:
            return "USABLE"
        if self.page.blocked:
            return "BLOCKED"
        if self.page.ok:
            return "THIN"
        return "FAILED"


def _signals(text: str) -> list[str]:
    found = []
    if _PRICE.search(text):
        found.append("price")
    if _AREA.search(text):
        found.append("area")
    if _ROOMS.search(text):
        found.append("rooms")
    return found


def _urls_from_argv_or_env() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    raw = os.environ.get("PARSER_PROBE_URLS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


async def probe(urls: list[str]) -> list[Verdict]:
    settings = get_settings()
    fetcher = build_fetcher(settings.parser)
    await fetcher.preflight()
    try:
        pages = await fetcher.fetch_many(urls)
    finally:
        await fetcher.aclose()

    return [
        Verdict(url=url, page=page, signals=_signals(page.text))
        for url, page in ((url, pages[url]) for url in urls if url in pages)
    ]


def report(verdicts: list[Verdict]) -> int:
    print()
    print(f"{'domain':24} {'verdict':9} {'status':7} {'chars':>7}  signals")
    print("-" * 72)
    for verdict in verdicts:
        status = str(verdict.page.status or "-")
        signals = ",".join(verdict.signals) or "-"
        print(
            f"{verdict.domain[:24]:24} {verdict.summary:9} {status:7} "
            f"{len(verdict.page.text):7}  {signals}"
        )
        if verdict.page.error:
            print(f"{'':24} └─ {verdict.page.error}")

    blocked = sorted({v.domain for v in verdicts if v.page.blocked})
    if blocked:
        print()
        print("These answered like bot protection. Skip the doomed HTTP attempt by adding")
        print("them to PARSER_BROWSER_DOMAINS (and set PARSER_BROWSER_ENABLED=true):")
        print()
        print(f"  PARSER_BROWSER_DOMAINS={','.join(blocked)}")

    thin = [v for v in verdicts if v.summary == "THIN"]
    if thin:
        print()
        print("Fetched but not usable -- extraction returned little or nothing that looks")
        print("like a listing. Open these by hand before trusting them as a source:")
        for verdict in thin:
            print(f"  {verdict.url}")

    unusable = [v for v in verdicts if not v.usable]
    print()
    print(f"{len(verdicts) - len(unusable)}/{len(verdicts)} usable")
    return 1 if unusable else 0


def main() -> int:
    urls = _urls_from_argv_or_env()
    if not urls:
        print(__doc__)
        print("No URLs given. Pass them as arguments or set PARSER_PROBE_URLS.")
        return 2
    return report(asyncio.run(probe(urls)))


if __name__ == "__main__":
    raise SystemExit(main())
