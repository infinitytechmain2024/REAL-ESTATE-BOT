from datetime import UTC, datetime

from bot.services.facebook.activity import is_recent
from bot.services.facebook.store import FacebookGroupStore


def test_activity_classifier_accepts_recent_relative_timestamp() -> None:
    assert is_recent("3 days", max_age_days=30)
    assert not is_recent("2 months", max_age_days=30)
    assert not is_recent(None, max_age_days=30)


def test_activity_classifier_accepts_recent_absolute_timestamp() -> None:
    now = datetime(2026, 9, 21, tzinfo=UTC)
    assert is_recent("20.09.2026", max_age_days=30, now=now)
    assert not is_recent("01.01.2025", max_age_days=30, now=now)


def test_group_store_upserts_group(tmp_path) -> None:
    store = FacebookGroupStore(str(tmp_path / "groups.sqlite3"))
    store.record(
        url="https://www.facebook.com/groups/example/",
        title="Example",
        last_post_text="2 days",
        access_state="accessible",
        membership_state="join_requested",
        active=True,
    )
    store.record(
        url="https://www.facebook.com/groups/example/",
        title="Example renamed",
        last_post_text="40 days",
        access_state="accessible",
        membership_state="accessible",
        active=False,
    )
    with store._connect() as db:
        row = db.execute(
            "SELECT title, last_post_text, membership_state, active "
            "FROM facebook_discovered_groups WHERE url = ?",
            ("https://www.facebook.com/groups/example/",),
        ).fetchone()
    assert row == ("Example renamed", "40 days", "accessible", 0)
