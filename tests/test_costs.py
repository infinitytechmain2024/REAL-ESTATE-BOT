"""LLM cost accounting and the daily hard limit."""

from __future__ import annotations

import pytest

from bot.config import LimitsSettings, LLMSettings
from bot.exceptions import BudgetExceededError
from bot.services.costs import CostGuard

pytestmark = pytest.mark.asyncio


class _FakeRepo:
    """Records what the ledger was asked to store."""

    enabled = True

    def __init__(self, today: float | None = 0.0) -> None:
        self.today = today
        self.rows: list[dict[str, object]] = []

    async def llm_cost_today(self) -> float | None:
        return self.today

    async def record_llm_usage(self, **kwargs: object) -> None:
        self.rows.append(kwargs)


def _guard(repo: _FakeRepo, *, limit: float = 1.0, notifier=None) -> CostGuard:  # type: ignore[no-untyped-def]
    return CostGuard(
        limits=LimitsSettings(daily_searches=99, daily_details=99, daily_cost_usd=limit),
        # $1 per million either way makes the arithmetic checkable by eye.
        llm=LLMSettings(price_prompt_usd_per_1m=1.0, price_completion_usd_per_1m=1.0),
        repo=repo,
        notifier=notifier,
    )


async def test_cost_is_tokens_times_the_configured_price() -> None:
    guard = _guard(_FakeRepo())
    cost = await guard.record(
        provider="openrouter",
        model="gpt-4o-mini",
        purpose="rank",
        prompt_tokens=500_000,
        completion_tokens=500_000,
    )
    assert cost == pytest.approx(1.0)


async def test_every_call_is_written_to_the_ledger() -> None:
    repo = _FakeRepo()
    guard = _guard(repo)
    await guard.record(
        provider="groq", model="llama", purpose="extract",
        prompt_tokens=10, completion_tokens=20, user_id=42,
    )
    assert len(repo.rows) == 1
    assert repo.rows[0]["user_id"] == 42
    assert repo.rows[0]["purpose"] == "extract"


async def test_spending_accumulates_across_calls() -> None:
    guard = _guard(_FakeRepo())
    for _ in range(3):
        await guard.record(
            provider="p", model="m", purpose="rank",
            prompt_tokens=100_000, completion_tokens=0,
        )
    assert guard.spent_usd == pytest.approx(0.3)


async def test_calls_are_allowed_while_under_the_limit() -> None:
    guard = _guard(_FakeRepo(), limit=1.0)
    await guard.record(
        provider="p", model="m", purpose="rank",
        prompt_tokens=100_000, completion_tokens=0,
    )
    guard.ensure_within_budget()  # no exception


async def test_the_pipeline_is_stopped_once_the_budget_is_spent() -> None:
    guard = _guard(_FakeRepo(), limit=1.0)
    await guard.record(
        provider="p", model="m", purpose="rank",
        prompt_tokens=1_000_000, completion_tokens=0,
    )
    with pytest.raises(BudgetExceededError):
        guard.ensure_within_budget()


async def test_a_zero_limit_means_no_limit() -> None:
    guard = _guard(_FakeRepo(), limit=0.0)
    await guard.record(
        provider="p", model="m", purpose="rank",
        prompt_tokens=100_000_000, completion_tokens=0,
    )
    guard.ensure_within_budget()  # no exception


async def test_the_admin_is_alerted_once_and_only_once() -> None:
    sent: list[str] = []

    async def notifier(text: str) -> None:
        sent.append(text)

    guard = _guard(_FakeRepo(), limit=0.5, notifier=notifier)
    for _ in range(3):
        await guard.record(
            provider="p", model="m", purpose="rank",
            prompt_tokens=1_000_000, completion_tokens=0,
        )
    assert len(sent) == 1


async def test_a_restart_does_not_hand_out_a_fresh_budget() -> None:
    """The running total is seeded from the ledger, not from zero."""
    guard = _guard(_FakeRepo(today=4.2), limit=5.0)
    await guard.prime()
    assert guard.spent_usd == pytest.approx(4.2)


async def test_a_ledger_write_failure_still_updates_the_running_total() -> None:
    """Losing the row is bad; losing the limit that stops a runaway is worse."""

    class _BrokenRepo(_FakeRepo):
        async def record_llm_usage(self, **kwargs: object) -> None:
            raise RuntimeError("postgrest is down")

    guard = _guard(_BrokenRepo(), limit=1.0)
    await guard.record(
        provider="p", model="m", purpose="rank",
        prompt_tokens=1_000_000, completion_tokens=0,
    )
    with pytest.raises(BudgetExceededError):
        guard.ensure_within_budget()
