#!/usr/bin/env python3
"""Run one conservative end-to-end request against the Maps sidecar.

This deliberately exercises the bot adapter (create job -> poll -> download)
instead of only checking that port 8080 is open. Use one keyword and a low
depth while validating a deployment::

    python scripts/maps_probe.py --lat 40.4168 --lon -3.7038 --depth 1

The script never starts Docker or a browser. The sidecar must already be
running, and its coordinates must be supplied explicitly for this smoke run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import GoogleMapsSettings
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.services.search.google_maps import GoogleMapsSource


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="real estate agency")
    parser.add_argument("--city", default="Madrid")
    parser.add_argument("--country", default="Spain")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("GOOGLE_MAPS_BASE_URL", "http://127.0.0.1:8080"),
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--extract-emails", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = GoogleMapsSettings(
        enabled=True,
        base_url=args.base_url,
        latitude=args.lat,
        longitude=args.lon,
        depth=args.depth,
        max_results=args.max_results,
        timeout_seconds=args.timeout,
        extract_emails=args.extract_emails,
    )
    parsed = ParsedQuery(
        mode=Mode.LAND,
        location=Location(city=args.city, country=args.country),
        keywords=[args.keyword],
    )
    source = GoogleMapsSource(settings)
    try:
        result = await source.search(parsed)
    finally:
        await source.aclose()

    print(f"failed={result.failed} hits={len(result.hits)}")
    for hit in result.hits:
        print(f"- {hit.title} | {hit.url}")
    for note in result.notes:
        print(f"note: {note}")
    return 1 if result.failed else 0


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
