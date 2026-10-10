"""Memory source checkpoints mirror durable source launch and import rules."""

import asyncio

import pytest

from bot.web_search.sources.base import SourceListing
from bot.web_search.store import MemoryWebStore


async def test_source_is_claimed_once_and_snapshot_survives_failure() -> None:
    store = MemoryWebStore()
    claims = await asyncio.gather(*(store.claim_source("c", "idealista") for _ in range(8)))
    assert sum(fresh for _, fresh in claims) == 1
    assert not (await store.claim_source("c", "idealista"))[1]
    await store.save_source_run("c", "idealista", "run", "dataset")
    listings = [SourceListing("https://idealista.com/inmueble/1/", "Terreno", plot_m2=2000)]
    await store.source_ready("c", "idealista", listings)
    listings.clear()
    row = (await store.source_runs("c"))[0]
    assert row.state == "ready" and row.listings[0].plot_m2 == 2000
    with pytest.raises(ValueError):
        await store.advance_source_import("c", "idealista", 2)
    await store.advance_source_import("c", "idealista", 1)
    await store.advance_source_import("c", "idealista", 0)
    await store.source_ready("c", "idealista", [])
    await store.finish_source("c", "idealista", "unavailable")
    row = (await store.source_runs("c"))[0]
    assert row.state == "failed" and row.error_code == "unavailable"
    assert row.import_offset == 1 and len(row.listings) == 1
    assert row.run_id == "run" and row.dataset_id == "dataset"
    assert not (await store.claim_source("c", "idealista"))[1]


async def test_source_run_callback_does_not_revert_completed_checkpoint() -> None:
    store = MemoryWebStore()
    await store.claim_source("c", "idealista")
    await store.save_source_run("c", "idealista", "run", "dataset")
    await store.source_ready("c", "idealista", [])
    await store.finish_source("c", "idealista")
    await store.save_source_run("c", "idealista", "other", "other")
    await store.save_source_run("c", "idealista", "run", "dataset")
    row = (await store.source_runs("c"))[0]
    assert row.state == "completed" and row.run_id == "run"
