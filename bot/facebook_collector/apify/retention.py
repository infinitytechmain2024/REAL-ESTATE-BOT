"""Retention of the Apify provider's raw dataset items (``apify_raw_items``, migration 046).

Rows older than ``APIFY_FACEBOOK_RAW_RETENTION_DAYS`` (default 7) are deleted; ``apify_runs`` -- the audit trail
of every run, its cost and its checkpoint -- is never touched, and neither are the posts already in
``collected_posts``. ``--dry-run`` only counts. The project has no scheduler of its own: run it daily from the VPS
(a systemd timer or cron), inside the facebook-collector image::

    docker compose run --rm facebook-runner python -m bot.facebook_collector.apify.retention --dry-run
    docker compose run --rm facebook-runner python -m bot.facebook_collector.apify.retention

Prints counts only, never item content. Exit code 0 on success.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any

DEFAULT_DAYS = 7
BATCH = 5000  # rows per delete statement: a big backlog never holds one long lock


@dataclass(frozen=True, slots=True)
class Cleanup:
    days: int
    expired: int
    deleted: int
    dry_run: bool

    def line(self) -> str:
        verb = "would delete" if self.dry_run else "deleted"
        return f"apify_raw_items older than {self.days} days: {self.expired} expired, {verb} {self.expired if self.dry_run else self.deleted}"


def retention_days(value: str | None) -> int:
    days = int(value) if value and value.strip() else DEFAULT_DAYS
    if not 1 <= days <= 365:
        raise ValueError("APIFY_FACEBOOK_RAW_RETENTION_DAYS must be 1-365")
    return days


async def cleanup(pool: Any, days: int, *, dry_run: bool) -> Cleanup:
    """Count (and, unless ``dry_run``, delete) the raw items imported more than ``days`` days ago."""
    if not 1 <= days <= 365:
        raise ValueError("retention must be 1-365 days")
    expired = int(await pool.fetchval(
        "select count(*) from apify_raw_items where imported_at < now() - make_interval(days => $1)", days))
    deleted = 0
    while not dry_run:
        status = await pool.execute(
            """delete from apify_raw_items where id in (
                   select id from apify_raw_items where imported_at < now() - make_interval(days => $1)
                    order by imported_at limit $2)""", days, BATCH)
        removed = int(status.rsplit(" ", 1)[-1])
        deleted += removed
        if removed < BATCH:
            break
    return Cleanup(days, expired, deleted, dry_run)


async def main(argv: list[str] | None = None) -> int:
    import asyncpg

    parser = argparse.ArgumentParser(description="Delete Apify raw dataset items past their retention.")
    parser.add_argument("--dry-run", action="store_true", help="only count what would be deleted")
    parser.add_argument("--days", type=int, default=None, help="override APIFY_FACEBOOK_RAW_RETENTION_DAYS")
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    days = args.days if args.days is not None else retention_days(os.environ.get("APIFY_FACEBOOK_RAW_RETENTION_DAYS"))
    pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
    try:
        print((await cleanup(pool, days, dry_run=args.dry_run)).line())
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
