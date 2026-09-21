#!/usr/bin/env python3
"""Read the local research archive: list, filter and export what was found.

Every search writes its whole run to ``ARCHIVE_DIR`` (see
``bot/services/archive.py``). This is the way to get it back out without
opening JSON by hand -- what has been found, at what price, where, and with
which contacts, across every run.

Usage:
    python scripts/archive_export.py                       # last 20 listings
    python scripts/archive_export.py --limit 200
    python scripts/archive_export.py --source facebook
    python scripts/archive_export.py --max-price 300000 --currency EUR
    python scripts/archive_export.py --format csv > listings.csv
    python scripts/archive_export.py --format json --limit 0 > listings.json
    python scripts/archive_export.py --runs                # one line per search

``--limit 0`` means everything. Filters apply before the limit, so
``--source facebook --limit 20`` is the twenty newest group posts, not the
group posts among the twenty newest listings.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.services.archive import LISTINGS_FILE, load_listings

CSV_COLUMNS = [
    "saved_at",
    "source",
    "mode",
    "title",
    "price",
    "price_value",
    "price_currency",
    "location",
    "area",
    "score",
    "budget_fit",
    "contacts",
    "url",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read the local research archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Archive directory (default: ARCHIVE_DIR from the environment/.env)",
    )
    parser.add_argument("--limit", type=int, default=20, help="Rows to show; 0 for all")
    parser.add_argument("--source", choices=("web", "facebook"), help="Only this source")
    parser.add_argument("--mode", choices=("land", "investors"), help="Only this search mode")
    parser.add_argument("--user", type=int, help="Only this Telegram user id")
    parser.add_argument("--min-price", type=float, help="Only listings priced at or above this")
    parser.add_argument("--max-price", type=float, help="Only listings priced at or below this")
    parser.add_argument("--currency", help="Only this currency, e.g. EUR")
    parser.add_argument(
        "--with-contacts", action="store_true", help="Only listings that carry a contact"
    )
    parser.add_argument("--format", choices=("text", "csv", "json"), default="text")
    parser.add_argument(
        "--runs",
        action="store_true",
        help="Summarise the per-search run files instead of listing objects",
    )
    return parser.parse_args(argv)


def archive_dir(explicit: str | None) -> Path:
    """The directory to read, preferring --dir over the configured one.

    Falls back to the setting so this script agrees with the running bot
    without being told where to look.
    """
    if explicit:
        return Path(explicit).expanduser()
    try:
        from bot.config import get_settings

        return Path(get_settings().archive.dir).expanduser()
    except Exception:  # noqa: BLE001 - a missing TELEGRAM_TOKEN must not block a read
        return Path("./data/research")


def keep(row: dict[str, Any], args: argparse.Namespace) -> bool:
    """Whether *row* passes every filter that was asked for."""
    if args.source and row.get("source") != args.source:
        return False
    if args.mode and row.get("mode") != args.mode:
        return False
    if args.user is not None and row.get("user_id") != args.user:
        return False
    if args.currency and (row.get("price_currency") or "").upper() != args.currency.upper():
        return False
    if args.with_contacts and not row.get("contacts"):
        return False

    price = row.get("price_value")
    if args.min_price is not None or args.max_price is not None:
        # A listing with no price cannot satisfy a price filter; excluding it
        # is the honest answer, not a silent pass.
        if price is None:
            return False
        if args.min_price is not None and price < args.min_price:
            return False
        if args.max_price is not None and price > args.max_price:
            return False
    return True


def print_text(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        bits = [row.get("price"), row.get("location"), row.get("area")]
        facts = " · ".join(str(bit) for bit in bits if bit)
        source = "FB" if row.get("source") == "facebook" else "web"
        print(f"[{row.get('saved_at', '')[:19]}] [{source}] {row.get('title') or '(без названия)'}")
        if facts:
            print(f"    {facts}")
        if row.get("contacts"):
            print(f"    ☎ {', '.join(str(c) for c in row['contacts'])}")
        print(f"    {row.get('url')}  (релевантность {row.get('score')}%)")
        print()


def print_csv(rows: list[dict[str, Any]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        flat["contacts"] = "; ".join(str(c) for c in row.get("contacts") or [])
        writer.writerow(flat)


def summarise_runs(root: Path, limit: int) -> int:
    """One line per search run: when, what, how much it found."""
    files = sorted(root.glob("*/*.json"), reverse=True)
    if not files:
        print(f"No run files under {root}", file=sys.stderr)
        return 1

    for path in files[:limit] if limit else files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{path.name}: unreadable ({exc})", file=sys.stderr)
            continue
        counts = doc.get("counts", {})
        request = (doc.get("request") or {}).get("text", "")
        print(
            f"[{doc.get('saved_at', '')[:19]}] {doc.get('mode')}  "
            f"raw={counts.get('raw')} unique={counts.get('unique')} "
            f"web={counts.get('web')} fb={counts.get('facebook')} "
            f"read={counts.get('read')} sent={len(doc.get('sent') or [])}"
        )
        print(f"    «{request}»")
        print(f"    {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = archive_dir(args.dir)

    if not root.exists():
        print(
            f"Archive directory {root} does not exist yet — run a search first, "
            "or pass --dir.",
            file=sys.stderr,
        )
        return 1

    if args.runs:
        return summarise_runs(root, args.limit)

    rows = [row for row in load_listings(root) if keep(row, args)]
    if not rows:
        print(
            f"No listings matched in {root / LISTINGS_FILE}.",
            file=sys.stderr,
        )
        return 1
    if args.limit:
        rows = rows[: args.limit]

    if args.format == "json":
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    elif args.format == "csv":
        print_csv(rows)
    else:
        print_text(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
