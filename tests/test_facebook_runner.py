"""facebook-runner: starts only requested batches, one at a time, and records the outcome."""

from __future__ import annotations

import pytest

from bot.facebook_collector.runner import CollectorRunner, Launch


class FakeStore:
    def __init__(self, *launches: tuple[str, str, str]) -> None:
        # (launch id, batch id, batch state)
        self.pending = [Launch(i, b) for i, b, _ in launches]
        self.batches = {b: s for _, b, s in launches}
        self.finished: dict[str, tuple[str, str | None, str | None]] = {}
        self.running = ["left-over"]

    async def recover_interrupted(self) -> int:
        count, self.running = len(self.running), []
        return count

    async def claim(self) -> Launch | None:
        return self.pending.pop(0) if self.pending else None

    async def batch_state(self, batch_id: str) -> str | None:
        return self.batches.get(batch_id)

    async def finish(self, launch_id: str, state: str, result: str | None = None, error: str | None = None) -> None:
        self.finished[launch_id] = (state, result, error)


@pytest.mark.asyncio
async def test_runs_a_queued_batch_and_records_the_collector_result() -> None:
    ran: list[str] = []

    async def run(batch_id: str) -> str:
        ran.append(batch_id)
        return "succeeded"

    store = FakeStore(("l1", "b1", "queued"))
    runner = CollectorRunner(store, run)
    assert await runner.step() is True
    assert await runner.step() is False
    assert ran == ["b1"]
    assert store.finished == {"l1": ("finished", "succeeded", None)}


@pytest.mark.asyncio
async def test_skips_a_batch_that_is_no_longer_queued() -> None:
    async def run(batch_id: str) -> str:  # pragma: no cover - must not run
        raise AssertionError("started a batch that was not queued")

    store = FakeStore(("l1", "b1", "cancelled"), ("l2", "b2", "running"))
    runner = CollectorRunner(store, run)
    await runner.step()
    await runner.step()
    assert store.finished == {"l1": ("skipped", None, "batch is cancelled"), "l2": ("skipped", None, "batch is running")}


@pytest.mark.asyncio
async def test_a_collector_error_is_recorded_and_the_next_batch_still_runs() -> None:
    async def run(batch_id: str) -> str:
        if batch_id == "b1":
            raise ValueError("queued Facebook batch with a ready Facebook profile was not found")
        return "human_verification_required"

    store = FakeStore(("l1", "b1", "queued"), ("l2", "b2", "queued"))
    runner = CollectorRunner(store, run)
    await runner.step()
    await runner.step()
    assert store.finished["l1"][0] == "failed" and store.finished["l1"][2].startswith("ValueError: queued Facebook batch")
    assert store.finished["l2"] == ("finished", "human_verification_required", None)


@pytest.mark.asyncio
async def test_serve_recovers_interrupted_launches_then_polls() -> None:
    sleeps: list[float] = []

    class Stop(Exception):
        pass

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise Stop

    async def run(batch_id: str) -> str:
        return "succeeded"

    store = FakeStore(("l1", "b1", "queued"))
    runner = CollectorRunner(store, run, poll_seconds=7, sleep=sleep)
    with pytest.raises(Stop):
        await runner.serve()
    assert store.running == [] and "l1" in store.finished and sleeps == [7]
