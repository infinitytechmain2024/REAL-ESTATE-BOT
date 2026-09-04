"""Metering and budget enforcement around the provider chain.

The structured path is the one worth testing: its tokens used to be dropped on
the floor, and a schema-repair round is a second paid call that has to be
counted too.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from bot.config import LimitsSettings, LLMSettings
from bot.exceptions import BudgetExceededError, LLMError
from bot.services.costs import CostGuard
from bot.services.llm.base import ChatMessage, LLMProvider, LLMResponse, Usage
from bot.services.llm.manager import LLMManager
from bot.services.llm.registry import _REGISTRY

pytestmark = pytest.mark.asyncio


class _Answer(BaseModel):
    value: str


class _StubProvider(LLMProvider):
    """Returns canned replies and counts how many calls it received."""

    name: ClassVar[str] = "stub"
    supports_json_mode: ClassVar[bool] = False

    def __init__(self, replies: list[str] | None = None, tokens: int = 1000) -> None:
        self.replies = replies or ['{"value": "ok"}']
        self.tokens = tokens
        self.calls = 0

    async def chat(self, messages: list[ChatMessage], **kwargs: Any) -> LLMResponse:
        text = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LLMResponse(
            text=text,
            model="stub-model",
            provider=self.name,
            usage=Usage(prompt_tokens=self.tokens, completion_tokens=self.tokens),
        )

    async def aclose(self) -> None:
        return None


class _FakeRepo:
    enabled = True

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def llm_cost_today(self) -> float | None:
        return 0.0

    async def record_llm_usage(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """An LLMManager whose only provider is a stub."""
    provider = _StubProvider()
    monkeypatch.setitem(_REGISTRY, "stub", lambda settings: provider)
    mgr = LLMManager(
        LLMSettings(
            provider="stub",
            max_retries=0,
            price_prompt_usd_per_1m=1.0,
            price_completion_usd_per_1m=1.0,
        )
    )
    return mgr, provider


def _guard(repo: _FakeRepo, limit: float) -> CostGuard:
    return CostGuard(
        limits=LimitsSettings(daily_searches=99, daily_details=99, daily_cost_usd=limit),
        llm=LLMSettings(price_prompt_usd_per_1m=1.0, price_completion_usd_per_1m=1.0),
        repo=repo,  # type: ignore[arg-type]
    )


async def test_a_plain_call_is_metered(manager) -> None:  # type: ignore[no-untyped-def]
    mgr, _ = manager
    repo = _FakeRepo()
    mgr.attach_cost_guard(_guard(repo, limit=10.0))

    await mgr.chat([ChatMessage.user("hi")], purpose="details", user_id=7)

    assert len(repo.rows) == 1
    assert repo.rows[0]["purpose"] == "details"
    assert repo.rows[0]["user_id"] == 7
    assert repo.rows[0]["prompt_tokens"] == 1000


async def test_a_structured_call_is_metered(manager) -> None:  # type: ignore[no-untyped-def]
    """This is the path whose usage used to be lost entirely."""
    mgr, _ = manager
    repo = _FakeRepo()
    mgr.attach_cost_guard(_guard(repo, limit=10.0))

    await mgr.chat_structured([ChatMessage.user("hi")], _Answer, purpose="extract", user_id=3)

    assert len(repo.rows) == 1
    assert repo.rows[0]["purpose"] == "extract"


async def test_a_schema_repair_round_is_metered_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repair call costs money as surely as the first one did."""
    provider = _StubProvider(replies=["not json at all", '{"value": "ok"}'])
    monkeypatch.setitem(_REGISTRY, "stub", lambda settings: provider)
    mgr = LLMManager(LLMSettings(provider="stub", max_retries=0))
    repo = _FakeRepo()
    mgr.attach_cost_guard(_guard(repo, limit=10.0))

    await mgr.chat_structured([ChatMessage.user("hi")], _Answer, purpose="extract")

    assert provider.calls == 2
    assert len(repo.rows) == 2


async def test_a_call_is_refused_once_the_budget_is_spent(manager) -> None:  # type: ignore[no-untyped-def]
    mgr, provider = manager
    guard = _guard(_FakeRepo(), limit=0.001)
    mgr.attach_cost_guard(guard)

    # The first call spends $0.002, which is over the $0.001 limit.
    await mgr.chat([ChatMessage.user("hi")], purpose="rank")
    calls_before = provider.calls

    with pytest.raises(BudgetExceededError):
        await mgr.chat([ChatMessage.user("hi")], purpose="rank")

    # The refusal must happen before the provider is reached, not after.
    assert provider.calls == calls_before


async def test_a_budget_error_is_not_an_llm_error() -> None:
    """The pipeline degrades around LLMError; degrading here would keep spending."""
    assert not issubclass(BudgetExceededError, LLMError)


async def test_metering_is_off_until_a_guard_is_attached(manager) -> None:  # type: ignore[no-untyped-def]
    """A manager built without a guard must still work -- it just does not meter."""
    mgr, _ = manager
    response = await mgr.chat([ChatMessage.user("hi")], purpose="rank")
    assert response.text
