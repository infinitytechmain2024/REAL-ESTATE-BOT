"""The bounded Main Orchestra command dispatcher."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from bot.campaign.architect import InvalidGoal, plan_campaign
from bot.campaign.models import TERMINAL_STATES, Campaign
from bot.campaign.spec import TaskSpec
from bot.campaign.status_text import DONE, NOTHING, campaign_label

from .models import ClaimedCommand, ClaimLost, CommandReceipt, CommandState, ConfirmedCommand
from .parser import (
    CommandValidationError,
    parse_campaign,
    parse_campaign_goal,
    parse_campaign_spec,
    parse_run,
    parse_scope,
)
from .store import DISPATCHER_ACTOR, PostgresOrchestraStore

if TYPE_CHECKING:
    from bot.campaign.store import CampaignStore
    from bot.operators import OperatorSet

log = logging.getLogger(__name__)
Notifier = Callable[[int, str], Awaitable[None]]
MAX_BACKOFF_SECONDS = 30.0
REAP_INTERVAL_SECONDS = 60.0
NOT_STOPPABLE = "Этот поиск уже завершён или недоступен."


def _task_spec(raw: dict[str, Any] | None) -> TaskSpec | None:
    """The queued ``spec=`` token as a ``TaskSpec``; an unreadable one rejects the command, nothing is stored."""
    if raw is None:
        return None
    try:
        return TaskSpec.model_validate(raw)
    except ValueError as exc:
        raise CommandValidationError("spec is not a valid task description") from exc


class OrchestraDispatcher:
    def __init__(self, store: PostgresOrchestraStore, *, operator_ids: frozenset[int], lease_seconds: int = 30, poll_seconds: float = 1.0, stale_batch_seconds: int = 900, notifier: Notifier | None = None, campaigns: CampaignStore | None = None, roles: OperatorSet | None = None) -> None:
        if not 10 <= lease_seconds <= 300 or not 0.1 <= poll_seconds <= 30 or not 300 <= stale_batch_seconds <= 86_400:
            raise ValueError("unsafe dispatcher settings")
        self.stale_batch_seconds = stale_batch_seconds
        self._last_reap = float("-inf")
        self.store, self.lease_seconds, self.poll_seconds = store, lease_seconds, poll_seconds
        self.notifier, self.operator_ids = notifier, operator_ids
        self.campaigns = campaigns
        # The shared, live OperatorSet: people with the ``user`` role may
        # create campaigns, read their status and cancel their own, nothing else.
        self.roles = roles
        self._stop = asyncio.Event()

    async def enqueue(self, command: ConfirmedCommand) -> CommandReceipt:
        return await self.store.enqueue(command)

    async def reap_if_due(self) -> None:
        now = asyncio.get_running_loop().time()
        if now - self._last_reap < REAP_INTERVAL_SECONDS:
            return
        self._last_reap = now
        for batch_id in await self.store.reap_stale_batches(stale_seconds=self.stale_batch_seconds):
            log.warning("orchestra.stale_batch_failed", extra={"batch_id": batch_id})

    async def process_once(self) -> bool:
        await self.reap_if_due()
        await self.store.reclaim_expired()
        item = await self.store.claim_next(lease_seconds=self.lease_seconds)
        if item is None:
            return False
        if item.user_id not in self.operator_ids and not self._user_campaign(item):
            # Defence in depth: also rejects commands queued before the
            # allowlist existed or by an operator who has since been removed.
            log.warning("orchestra.not_operator", extra={"command_id": item.id, "user_id": item.user_id})
            await self._fail(item, "not_operator", f"telegram user {item.user_id} is not an operator")
            await self._problem(item, f"Orchestra: request {item.id} was rejected; only operators can change acquisition state.")
            return True
        plain = self._plain(item)
        if not plain:
            await self._notify(item.chat_id, f"Orchestra: processing {item.command} request {item.id}.")
        try:
            result = await self._dispatch(item)
            if plain:  # the campaign's own status message carries the progress
                if result.get("user_reply"):
                    await self._notify(item.chat_id, result["user_reply"])
            else:
                await self._notify(item.chat_id, result.get("reply") or f"Orchestra: {item.command} request {item.id} {_summary(result)}.")
        except ClaimLost:
            log.warning("orchestra.claim_lost", extra={"command_id": item.id})
            await self._problem(item, f"Orchestra: request {item.id} was cancelled before it completed; nothing was planned.")
        except InvalidGoal as exc:
            await self._fail(item, "invalid_goal", str(exc))
            await self._notify(item.chat_id, str(exc))  # written for the person who asked
        except CommandValidationError as exc:
            await self._fail(item, "invalid_command", str(exc))
            await self._problem(item, f"Orchestra: request {item.id} failed validation: {exc}")
        except ValueError as exc:
            await self._fail(item, "precondition_failed", str(exc))
            await self._problem(item, f"Orchestra: request {item.id} cannot run: {exc}")
        except Exception as exc:
            log.exception("orchestra.dispatch_failed", extra={"command_id": item.id})
            await self._fail(item, type(exc).__name__, str(exc)[:500])
            await self._problem(item, f"Orchestra: request {item.id} failed safely; inspect its audit record.")
        return True

    def _is_owner(self, user_id: int) -> bool:
        # Without the shared OperatorSet (older wiring, tests) every controller counts as an owner.
        return self.roles.is_owner(user_id) if self.roles is not None else user_id in self.operator_ids

    def _plain(self, item: ClaimedCommand) -> bool:
        """A campaign command from anyone but an owner: no ids, queue lines or English in its replies."""
        return item.command == "campaign" and not self._is_owner(item.user_id)

    async def _problem(self, item: ClaimedCommand, technical: str) -> None:
        """Owners get the technical line; a normal user a plain status, and the owners the details."""
        if not self._plain(item):
            await self._notify(item.chat_id, technical)
            return
        await self._notify(item.chat_id, NOTHING)
        for owner in sorted(self.roles.owners if self.roles is not None else ()):
            if owner != item.chat_id:
                await self._notify(owner, f"{technical} (telegram:{item.user_id})")

    async def _dispatch(self, item: ClaimedCommand) -> dict[str, Any]:
        """Plan or apply the command; the store finishes it in the same transaction."""
        actor = f"telegram:{item.user_id}"
        if item.command == "run":
            return await self.store.plan_run(parse_run(item.arguments), item, actor=actor)
        if item.command == "campaign":
            return await self._campaign(item, actor, own_only=item.user_id not in self.operator_ids)
        scope_kind, identifier = parse_scope(item.arguments)
        return await self.store.apply_lifecycle(item, scope_kind, identifier, actor=actor)

    def _user_campaign(self, item: ClaimedCommand) -> bool:
        return item.command == "campaign" and self.roles is not None and self.roles.role(item.user_id) == "user"

    async def _campaign(self, item: ClaimedCommand, actor: str, *, own_only: bool = False) -> dict[str, Any]:
        """Plan and store a campaign, cancel one, or report the chat's latest; the runner does the work.

        ``own_only`` (the ``user`` role): status and cancel see only campaigns this person requested.
        """
        if self.campaigns is None:
            raise ValueError("campaigns are not available in this service")
        action, value = parse_campaign(item.arguments)
        created: str | None = None
        user_reply: str | None = None  # what a non-owner sees; None: the status message says it
        if action == "status":
            campaign = await self.campaigns.latest_for_chat(item.chat_id)
            if own_only and campaign is not None and campaign.requested_by != item.user_id:
                campaign = None
            result = {"status": "finished", "campaign_id": campaign.id if campaign else None, "reply": campaign_status(campaign)}
            user_reply = user_campaign_status(campaign)
        elif action == "cancel" and own_only and not await self._owns(value, item.user_id):
            log.warning("orchestra.campaign_cancel_refused", extra={"command_id": item.id, "user_id": item.user_id})
            result = {"status": "refused", "campaign_id": value, "reply": f"Можно остановить только свою кампанию; {value} не ваша или не найдена."}
            user_reply = NOT_STOPPABLE
        elif action == "cancel":
            cancelled = await self.campaigns.cancel(value, actor)
            reply = f"Кампания {value} остановлена." if cancelled else f"Кампания {value} не найдена или уже завершена."
            result = {"status": "cancelled" if cancelled else "unchanged", "campaign_id": value, "reply": reply}
            # A user stops a search with «стоп»: the control plane already said «Поиск остановлен.»
            # and the status message turns final, so only a search that had already ended gets a line.
            user_reply = NOT_STOPPABLE if not cancelled else None if own_only else DONE
        else:
            # Intake queues "mode=<vertical> place=<names> <task>": the person's choices override detection.
            # ... and, from the interviewer, "spec=<TaskSpec JSON>": the confirmed requirements.
            goal, vertical, city, place = parse_campaign_goal(value)
            spec = _task_spec(parse_campaign_spec(value))
            plan = plan_campaign(goal, vertical=vertical, location=city, place=place,  # type: ignore[arg-type]  # InvalidGoal: nothing is stored
                                 spec=spec)
            extra: dict[str, Any] = {"spec": spec.model_dump(mode="json")} if spec is not None else {}
            created = await self.campaigns.create(plan, chat_id=item.chat_id, requested_by=item.user_id,
                                                  source_text=goal, actor=actor, **extra)
            result = {"status": "planned", "campaign_id": created, "reply": f"Кампания {created} запланирована: {plan.goal}"}
        try:
            await self.store.complete(item, CommandState.FINISHED, result)
        except ClaimLost:
            if created is not None:  # the command was cancelled meanwhile: so is its campaign
                await self.campaigns.cancel(created, DISPATCHER_ACTOR)
            raise
        return {**result, "user_reply": user_reply}

    async def _owns(self, campaign_id: str, user_id: int) -> bool:
        assert self.campaigns is not None
        campaign = await self.campaigns.get(campaign_id)
        return campaign is not None and campaign.requested_by == user_id

    async def _fail(self, item: ClaimedCommand, error_code: str, error_detail: str) -> None:
        with suppress(ClaimLost):
            await self.store.complete(item, CommandState.FAILED, {"status": "failed"}, error_code=error_code, error_detail=error_detail)

    async def run_forever(self) -> None:
        """Keep draining the inbox; a database outage delays work but never ends the loop."""
        failures = 0
        while not self._stop.is_set():
            try:
                worked = await self.process_once()
                failures, delay = 0, self.poll_seconds
            except Exception:
                failures += 1
                worked, delay = False, min(MAX_BACKOFF_SECONDS, self.poll_seconds * 2 ** min(failures, 6))
                log.exception("orchestra.loop_failed", extra={"consecutive_failures": failures, "retry_in_seconds": delay})
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)

    def stop(self) -> None:
        self._stop.set()

    async def _notify(self, chat_id: int, text: str) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(chat_id, text)
        except Exception:  # noqa: BLE001 - Telegram delivery must not undo durable orchestration.
            log.warning("orchestra.status_notification_failed", extra={"chat_id": chat_id})


_STATE_NAMES = {
    "planned": "запланирована", "discovering": "поиск групп", "running": "идёт сбор",
    "paused_verification": "нужна verification", "completed": "завершена", "cancelled": "остановлена",
    "failed": "ошибка",
}


def campaign_status(campaign: Campaign | None) -> str:
    if campaign is None:
        return "В этом чате ещё нет кампаний. Начните: /campaign <что и где искать>"
    reason = f" ({campaign.stop_reason})" if campaign.stop_reason and campaign.state in ("failed", "paused_verification") else ""
    return f"Кампания {campaign.id}: {_STATE_NAMES.get(campaign.state, campaign.state)}{reason}\n{campaign.plan.goal}"


def user_campaign_status(campaign: Campaign | None) -> str:
    """``/campaign status`` for a non-owner: one of the user-safe labels."""
    if campaign is None:
        return NOTHING
    return DONE if campaign.state in TERMINAL_STATES else campaign_label(campaign.state)


def _summary(result: dict[str, Any]) -> str:
    if result.get("batch_id"):
        return f"queued Facebook batch {result['batch_id']} ({result.get('max_groups')} groups); it starts automatically"
    if result.get("run_id"):
        return f"queued run {result['run_id']}; it starts automatically"
    return f"{result.get('status', 'finished')} ({result.get('affected', 0)} affected)"
