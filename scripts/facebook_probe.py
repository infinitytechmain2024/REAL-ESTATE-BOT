#!/usr/bin/env python3
"""Day-1 validation: open one real group, search it, and show what we actually got.

This is the acceptance test the implementation plan calls for before any of
``bot/services/facebook/`` is trusted: "real search results from the
selected group are saved with working links and manual comparison passes."
Run it, then open the printed post URLs by hand and check the text matches.

Usage:
    python scripts/facebook_probe.py "<group url>" "<search query>"

The first run needs a logged-in session: with FACEBOOK_HEADLESS=false (the
default), a real browser window opens on this machine. Log into Facebook by
hand in that window the first time -- the persistent profile
(FACEBOOK_PROFILE_DIR) keeps the session for every run after that.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import get_settings
from bot.services.facebook.browser import FacebookSession, SessionState
from bot.services.facebook.groups import GroupAccess, check_access, search_posts


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    group_url, query = sys.argv[1], sys.argv[2]

    settings = get_settings().facebook
    session = FacebookSession(settings)
    await session.start()
    try:
        page = session.page
        print("-> checking session state...")
        state = await session.probe_state()
        print(f"   session: {state.value}")
        if state == SessionState.LOGIN_NEEDED:
            print("   Not logged in. Log in by hand in the opened browser window, then re-run.")
            return 1
        if state == SessionState.HUMAN_REQUIRED:
            print("   Facebook wants verification (checkpoint / unrecognised page).")
            print("   Resolve it by hand in the opened browser window, then re-run.")
            return 1

        print(f"-> opening group: {group_url}")
        access = await check_access(page, group_url)
        print(f"   access: {access.value}")
        if access != GroupAccess.ACCESSIBLE:
            print(
                "   Not accessible -- nothing to read. This is exactly the case the admin "
                "queue needs to surface once it exists."
            )
            return 1

        print(f"-> searching group for: {query!r}")
        posts = await search_posts(page, group_url, query, max_posts=settings.max_posts_per_group)
        print(f"   found {len(posts)} post(s)")

        out = Path(__file__).resolve().parent.parent / "data" / "facebook_probe_output.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([p.model_dump() for p in posts], ensure_ascii=False, indent=2))
        print(f"-> wrote {out}")

        for post in posts:
            print(f"   - {post.post_url}")
            print(f"     {post.text[:120]!r}")

        print()
        print("Now open a few of those post_url values by hand and confirm the text matches.")
        return 0
    finally:
        await session.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
