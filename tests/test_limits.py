"""Cooldowns, concurrency slots and daily quotas."""

from __future__ import annotations

import pytest

from bot.config import LimitsSettings
from bot.exceptions import QuotaExceededError
from bot.middlewares.throttling import Cooldown, SearchSlots, SlotDenial
from bot.services.limits import QuotaKind, QuotaService

pytestmark = pytest.mark.asyncio


# -- Cooldown -------------------------------------------------------------


async def test_cooldown_lets_the_first_call_through() -> None:
    cooldown = Cooldown(10.0)
    assert cooldown.try_pass(1) == 0.0


async def test_cooldown_blocks_the_second_call() -> None:
    cooldown = Cooldown(10.0)
    cooldown.try_pass(1)
    assert cooldown.try_pass(1) > 0


async def test_a_refusal_does_not_extend_the_window() -> None:
    """Otherwise button-mashing would keep pushing the window forward forever."""
    cooldown = Cooldown(10.0)
    cooldown.try_pass(1)
    first = cooldown.try_pass(1)
    second = cooldown.try_pass(1)
    assert second <= first


async def test_cooldowns_are_per_user() -> None:
    cooldown = Cooldown(10.0)
    cooldown.try_pass(1)
    assert cooldown.try_pass(2) == 0.0


async def test_a_zero_cooldown_never_blocks() -> None:
    cooldown = Cooldown(0.0)
    assert cooldown.try_pass(1) == 0.0
    assert cooldown.try_pass(1) == 0.0


# -- SearchSlots ----------------------------------------------------------


async def test_a_user_cannot_run_two_searches_at_once() -> None:
    slots = SearchSlots(limit=5, per_user_limit=1)
    assert slots.try_acquire(1) is None
    assert slots.try_acquire(1) is SlotDenial.USER


async def test_the_global_cap_holds_across_users() -> None:
    slots = SearchSlots(limit=2, per_user_limit=1)
    assert slots.try_acquire(1) is None
    assert slots.try_acquire(2) is None
    assert slots.try_acquire(3) is SlotDenial.GLOBAL


async def test_releasing_frees_both_counters() -> None:
    slots = SearchSlots(limit=1, per_user_limit=1)
    slots.try_acquire(1)
    slots.release(1)
    assert slots.active == 0
    assert slots.active_for(1) == 0
    assert slots.try_acquire(2) is None


async def test_release_does_not_go_negative() -> None:
    """A double release must not hand out a free slot."""
    slots = SearchSlots(limit=1, per_user_limit=1)
    slots.try_acquire(1)
    slots.release(1)
    slots.release(1)
    assert slots.active == 0


async def test_per_user_map_does_not_leak_entries() -> None:
    slots = SearchSlots(limit=5, per_user_limit=1)
    for user_id in range(100):
        slots.try_acquire(user_id)
        slots.release(user_id)
    assert slots._per_user == {}


# -- QuotaService ---------------------------------------------------------


class _FakeRepo:
    """A repository that counts in memory, standing in for Supabase."""

    def __init__(self, *, enabled: bool = True, broken: bool = False) -> None:
        self.enabled = enabled
        self.broken = broken
        self.counts: dict[tuple[int, str], int] = {}

    async def bump_daily_usage(self, user_id: int, kind: str, amount: int = 1) -> int | None:
        if self.broken or not self.enabled:
            return None
        key = (user_id, kind)
        self.counts[key] = self.counts.get(key, 0) + amount
        return self.counts[key]


def _service(repo: _FakeRepo, **kwargs: object) -> QuotaService:
    settings = LimitsSettings(daily_searches=3, daily_details=2, daily_cost_usd=0, **kwargs)  # type: ignore[arg-type]
    return QuotaService(settings, repo)  # type: ignore[arg-type]


async def test_requests_within_the_quota_are_allowed() -> None:
    quota = _service(_FakeRepo())
    for expected in (1, 2, 3):
        verdict = await quota.consume(1, QuotaKind.SEARCHES)
        assert verdict.allowed and verdict.used == expected


async def test_the_request_past_the_quota_is_refused() -> None:
    quota = _service(_FakeRepo())
    for _ in range(3):
        await quota.consume(1, QuotaKind.SEARCHES)
    with pytest.raises(QuotaExceededError):
        await quota.consume(1, QuotaKind.SEARCHES)


async def test_quotas_are_per_user() -> None:
    quota = _service(_FakeRepo())
    for _ in range(3):
        await quota.consume(1, QuotaKind.SEARCHES)
    verdict = await quota.consume(2, QuotaKind.SEARCHES)
    assert verdict.allowed


async def test_searches_and_details_are_counted_separately() -> None:
    quota = _service(_FakeRepo())
    for _ in range(3):
        await quota.consume(1, QuotaKind.SEARCHES)
    assert (await quota.consume(1, QuotaKind.DETAILS)).allowed


async def test_the_limit_still_holds_when_supabase_is_down() -> None:
    """Falling back to an in-process counter beats failing open."""
    quota = _service(_FakeRepo(broken=True))
    for _ in range(3):
        await quota.consume(1, QuotaKind.SEARCHES)
    with pytest.raises(QuotaExceededError):
        await quota.consume(1, QuotaKind.SEARCHES)


async def test_a_zero_limit_refuses_without_touching_the_database() -> None:
    repo = _FakeRepo()
    settings = LimitsSettings(daily_searches=0, daily_details=5, daily_cost_usd=0)
    quota = QuotaService(settings, repo)  # type: ignore[arg-type]
    with pytest.raises(QuotaExceededError):
        await quota.consume(1, QuotaKind.SEARCHES)
    assert repo.counts == {}
