"""Migrations 031 and 035: user_task_drafts.step accepts the steps the intake saves; the TaskSpec is kept on the draft and the campaign.

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.control_plane.intake import Draft, PostgresIntakeStore

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"), reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))


async def test_ask_and_target_steps_are_saved() -> None:
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=2)
    try:
        class Owner:
            def _pool(self):
                return pool

        store = PostgresIntakeStore(Owner())
        for step in ("ask", "target", "summary", "idle"):
            await store.save(Draft(user_id=7, chat_id=-1, mode="investors", step=step))
            saved = await store.get(7)
            assert saved is not None and saved.step == step
        with pytest.raises(asyncpg.CheckViolationError):
            await store.save(Draft(user_id=7, chat_id=-1, mode="investors", step="bogus"))
    finally:
        await pool.close()


async def test_the_draft_keeps_its_spec_and_a_campaign_stores_it() -> None:
    """Migration 035: the TaskSpec survives save/get, is handed over by launch, and lands on the campaign."""
    import asyncpg

    from bot.campaign import PostgresCampaignStore, plan_campaign
    from bot.campaign.spec import TaskSpec

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=2)
    try:
        class Owner:
            def _pool(self):
                return pool

        store = PostgresIntakeStore(Owner())
        spec = TaskSpec(mode="real_estate").merged({
            "place": {"name": "Valencia", "country": "ES", "names": {"ru": "Валенсия"}}, "deal": "rent",
            "property_type": "apartment", "budget": {"max": 900, "currency": "EUR"}, "rooms": {"min": 2},
            "wishes": [{"text": "балкон", "weight": 3}], "unspecified": ["area_m2.min"]})
        draft = Draft(user_id=7, chat_id=-1, mode="real_estate", step="ask", task="квартира в Валенсии", message_id=3,
                      spec=spec, dialogue=[{"role": "user", "text": "квартира в Валенсии"}], rounds=2,
                      asking="budget.max", forced=True)
        await store.save(draft)
        loaded = await store.get(7)
        assert loaded is not None and loaded.spec == spec and loaded.rounds == 2 and loaded.forced
        assert (loaded.asking, loaded.dialogue) == ("budget.max", draft.dialogue)

        # A draft without a spec (a task not yet started) round-trips as None.
        await store.save(Draft(user_id=8, chat_id=-1, mode="investors"))
        empty = await store.get(8)
        assert empty is not None and empty.spec is None

        await store.save(draft.copy(step="summary"))
        taken = await store.launch(7)
        assert taken is not None and taken.spec == spec and taken.task == "квартира в Валенсии"
        after = await store.get(7)
        assert after is not None and after.step == "idle" and after.spec is None  # reset in the same statement
        assert await store.launch(7) is None  # a second tap gets nothing

        campaigns = PostgresCampaignStore(pool)
        plan = plan_campaign("аренда квартира в Валенсии", vertical="real_estate", location="Valencia", spec=spec)
        with_spec = await campaigns.create(plan, chat_id=-1, requested_by=7, source_text="x", actor="t",
                                           spec=spec.model_dump(mode="json"))
        without = await campaigns.create(plan, chat_id=-2, requested_by=7, source_text="x", actor="t")
        stored = await campaigns.get(with_spec)
        assert stored is not None and TaskSpec.model_validate(stored.spec) == spec
        assert stored.plan.constraints["max_price"] == 900 and stored.plan.constraints["rooms"] == 2
        latest = await campaigns.latest_for_chat(-1)
        assert latest is not None and latest.spec == stored.spec
        none = await campaigns.get(without)
        assert none is not None and none.spec is None
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("update public.campaigns set spec = '[1]'::jsonb where id = $1::uuid", with_spec)
    finally:
        await pool.close()
