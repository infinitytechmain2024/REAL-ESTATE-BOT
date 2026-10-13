#!/usr/bin/env python3
"""Delete Apify raw dataset items (apify_raw_items) older than APIFY_FACEBOOK_RAW_RETENTION_DAYS.

    DATABASE_URL=... python3 scripts/cleanup_apify_raw.py --dry-run   # count only
    DATABASE_URL=... python3 scripts/cleanup_apify_raw.py             # delete

A host-side entry point for bot/facebook_collector/apify/retention.py (which also runs inside the
facebook-collector image: ``python -m bot.facebook_collector.apify.retention``). apify_runs, the audit trail,
is never touched. Needs the project's requirements (asyncpg).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.facebook_collector.apify.retention import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
