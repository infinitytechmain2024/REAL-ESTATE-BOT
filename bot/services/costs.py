"""LLM cost accounting and the daily hard limit.

Every model call is priced from the token counts the provider reports and the
per-million prices in ``LLM_PRICE_*``, written to the ``llm_usage`` ledger, and
added to a running total for the current UTC day. Once that total reaches
``LIMITS_DAILY_COST_USD`` the guard refuses to let another call start: the
pipeline stops with a :class:`~bot.exceptions.BudgetExceededError` and the
admins are told once.

The running total lives in memory and is seeded from Supabase at start-up, so
a restart does not hand the deployment a fresh budget. It is deliberately
*not* re-read before every call -- that would be a database round trip per LLM
request to defend against a second worker that this deployment does not run.

The figure is an estimate. Providers return tokens, not prices, so this is
tokens x configured price: right for stopping a runaway, not a substitute for
the provider's own billing page.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bot.config import LimitsSettings, LLMSettings
from bot.exceptions import BudgetExceededError
from bot.logging_conf import get_logger
from bot.services.limits import utc_today

if TYPE_CHECKING:
    from bot.services.db import SupabaseRepository

log = get_logger(__name__)

Notifier = Callable[[str], Awaitable[None]]
"""Sends an alert to the admins; supplied by ``bot.main`` once the Bot exists."""


class CostGuard:
    """Tracks what the deployment spends on the LLM and stops it at the limit."""

    def __init__(
        self,
        *,
        limits: LimitsSettings,
        llm: LLMSettings,
        repo: SupabaseRepository,
        notifier: Notifier | None = None,
    ) -> None:
        self.limits = limits
        self.llm = llm
        self.repo = repo
        self.notifier = notifier
        self._date: dt.date = utc_today()
        self._spent_usd = 0.0
        self._alerted = False

    @property
    def spent_usd(self) -> float:
        """Estimated spend so far today."""
        self._roll_over()
        return self._spent_usd

    @property
    def limit_usd(self) -> float:
        return self.limits.daily_cost_usd

    async def prime(self) -> None:
        """Seed today's total from Supabase, so a restart is not a fresh budget."""
        if not self.limits.cost_limit_enabled:
            return
        total = await self.repo.llm_cost_today()
        if total is None:
            log.info(
                "costs.prime.unavailable",
                detail="starting today's total at 0; the ledger could not be read",
            )
            return
        self._date = utc_today()
        self._spent_usd = total
        log.info("costs.primed", spent_usd=round(total, 4), limit_usd=self.limit_usd)

    def ensure_within_budget(self) -> None:
        """Raise :class:`BudgetExceededError` if the day's budget is already spent.

        Called before every LLM request. Cheap on purpose -- it is a comparison
        against a number this process already has.
        """
        if not self.limits.cost_limit_enabled:
            return
        self._roll_over()
        if self._spent_usd >= self.limits.daily_cost_usd:
            raise BudgetExceededError(
                f"daily LLM budget spent: ${self._spent_usd:.4f} of ${self.limits.daily_cost_usd:.2f}"
            )

    async def record(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
        user_id: int | None = None,
    ) -> float:
        """Price one completed call, log it, store it, and return its cost.

        Never raises: the call has already happened and been paid for, so
        failing to write the ledger row must not also lose the user's answer.
        The in-memory total is updated first, so the limit still tightens even
        when the write fails.
        """
        cost = self.llm.cost_usd(prompt_tokens, completion_tokens)
        self._roll_over()
        self._spent_usd += cost

        log.info(
            "llm.cost",
            provider=provider,
            model=model,
            purpose=purpose,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=round(cost, 6),
            spent_today_usd=round(self._spent_usd, 4),
            limit_usd=self.limit_usd or None,
        )

        try:
            await self.repo.record_llm_usage(
                user_id=user_id,
                provider=provider,
                model=model,
                purpose=purpose,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
            )
        except Exception:  # noqa: BLE001 - the ledger is never worth failing a request over
            log.warning("costs.ledger_write_failed", provider=provider, exc_info=True)

        await self._maybe_alert()
        return cost

    async def _maybe_alert(self) -> None:
        """Tell the admins, once per day, that the budget has run out."""
        if not self.limits.cost_limit_enabled or self._alerted:
            return
        if self._spent_usd < self.limits.daily_cost_usd:
            return

        self._alerted = True
        log.error(
            "costs.budget_exceeded",
            spent_usd=round(self._spent_usd, 4),
            limit_usd=self.limits.daily_cost_usd,
        )
        if self.notifier is None:
            return
        try:
            await self.notifier(
                "🛑 <b>Дневной бюджет на ИИ-запросы исчерпан</b>\n\n"
                f"Потрачено: <b>${self._spent_usd:.2f}</b> из ${self.limits.daily_cost_usd:.2f}\n"
                f"Дата (UTC): {self._date.isoformat()}\n\n"
                "Поиск остановлен до 00:00 UTC. "
                "Чтобы поднять лимит, измените <code>LIMITS_DAILY_COST_USD</code> и перезапустите бота."
            )
        except Exception:  # noqa: BLE001 - a failed alert must not break the caller
            log.warning("costs.alert_failed", exc_info=True)

    def _roll_over(self) -> None:
        """Reset the total when the UTC date changes."""
        today = utc_today()
        if today != self._date:
            log.info(
                "costs.rolled_over",
                previous=self._date.isoformat(),
                spent_usd=round(self._spent_usd, 4),
            )
            self._date = today
            self._spent_usd = 0.0
            self._alerted = False
