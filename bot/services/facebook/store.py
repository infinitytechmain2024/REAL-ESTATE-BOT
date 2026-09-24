"""Local persistence for discovered Facebook groups."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class FacebookGroupStore:
    """Tiny SQLite store; Supabase remains optional for search results."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS facebook_discovered_groups (
                    url TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    last_post_text TEXT,
                    last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    access_state TEXT NOT NULL,
                    membership_state TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(
        self,
        *,
        url: str,
        title: str,
        last_post_text: str | None,
        access_state: str,
        membership_state: str,
        active: bool,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO facebook_discovered_groups
                   (url, title, last_post_text, access_state, membership_state, active)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     title=excluded.title,
                     last_post_text=excluded.last_post_text,
                     last_checked_at=CURRENT_TIMESTAMP,
                     access_state=excluded.access_state,
                     membership_state=excluded.membership_state,
                     active=excluded.active""",
                (url, title, last_post_text, access_state, membership_state, int(active)),
            )
