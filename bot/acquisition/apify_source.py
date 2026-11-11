"""One paid Apify run of a v3 social source (Instagram, TikTok, LinkedIn, Facebook reactions), from claim to import.

Nothing calls it yet: the sources (P1.2-P1.8) build an :class:`ApifySocialRun` behind their own flags and hand it
an actor input and a sink for the dataset items. The journal is ``apify_source_runs`` (migration 051).

The life of one run, safe to repeat after any crash (the same rules as ``bot.campaign.post_leads`` and
``bot.facebook_collector.apify.provider``):

1. **claim** the row ``(campaign, purpose, input hash)``: the same input for the same campaign is launched at most
   once. A row whose ``run_id`` is set is never launched again;
2. the campaign budget (``costs.over_budget``) is checked before a launch, never before a resume;
3. the row gets ``run_id = pending:<uuid>`` **before** the paid launch (``docs/v2/HANDOFF.md`` §4 item 2);
4. **launch**: one POST with ``maxTotalChargeUsd`` and the pinned build (``ApifyHttp.launch``, never retried).
   A lost answer (``LaunchUnknown``) is never launched again: the row closes ``failed launch_unconfirmed`` and the
   cap is booked as an estimate. An explicit refusal (4xx, 429) removes the placeholder: nothing was started. Only
   ``RETRYABLE`` refusals (402, 429) are tried again, at most ``max_attempts`` launches; any other 4xx stays failed.
   With ``rate_limit_seconds`` set, a 429 (Apify's rate limit: nothing started, nothing paid) does not spend an
   attempt: the launch is tried again within the same call every ``rate_limit_pause_seconds``, for at most
   ``rate_limit_seconds``; a 429 that outlasts that window spends one attempt (so the whole wait for one input is
   bounded by ``max_attempts × rate_limit_seconds``). 0 (the default): every 429 is an attempt.
   A ``launching`` row younger than ``launch_grace_seconds`` is another worker's launch in flight (``busy``);
5. **checkpoint** the real run id (if that write fails, the run is aborted and booked at its cap); then the cap is booked as an estimate under ``apify:<run_id>`` and the run awaited;
6. **bill** once: ``costs.record_unique`` under ``apify:<run_id>`` with ``usageTotalUsd``, or the cap with
   ``code='estimated'`` when the run did not say (an estimate never replaces a known cost);
7. **import** the dataset page by page (``ApifyHttp.dataset_page``) from the stored offset; each page goes to the
   source's sink, then the offset is saved;
8. a run that ``SUCCEEDED`` with ``statusMessage`` «max charge reached» stopped on its ``maxTotalChargeUsd``: with no
   dataset row at all the cap was too low for the actor to do anything (P1.7 sample 2026-11-11,
   ``harvestapi/linkedin-profile-search`` at $0.15), so the row closes ``failed apify_cap_too_low`` (billed by
   ``usageTotalUsd`` as any other run), never as an empty success; with rows it is a success marked ``capped``.

A page can reach the sink twice (a crash between the sink and the checkpoint), so a sink must be idempotent (as
``collected_posts`` inserts are: ``on conflict do nothing``). An error that is not an Apify one (the database, the
sink) leaves the row as it is and propagates: the next call resumes it, never relaunches it.

No cookie, token or session value is passed to an actor or kept from its items (``scrub``); the token never appears
in a log line (``ApifyHttp``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from bot.acquisition.apify_client import (
    TERMINAL,
    ApifyError,
    ApifyHttp,
    LaunchUnknown,
    TemporaryApifyError,
    actor_path,
    check_pinned_build,
)
from bot.acquisition.apify_util import PENDING, scrub
from bot.acquisition.contracts import Platform
from bot.utils import costs

if TYPE_CHECKING:
    import asyncpg

log = logging.getLogger(__name__)

ACTOR = "apify_source"
STATUSES = ("claimed", "launching", "running", "succeeded", "failed")
_PURPOSE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_:-")
# A sink gets one page of scrubbed items and the run id, and answers how many it kept.
Sink = Callable[[list[dict[str, Any]], str], Awaitable[int]]
Outcome = Literal["succeeded", "failed", "skipped", "retry", "busy"]
# Refusals of a launch that may pass later (quota, account balance): the row is opened again, up to max_attempts.
# Every other 4xx (400, 404, 422…) means this input cannot run: the row stays failed. A 5xx or a lost answer on the
# launch is LaunchUnknown, never retried.
RETRYABLE = frozenset({"apify_http_402", "apify_http_429"})
RATE_LIMITED = "apify_http_429"
# The run object's ``statusMessage`` when an actor stopped on its maxTotalChargeUsd (it still ends SUCCEEDED).
CAP_REACHED = "max charge reached"
CAP_TOO_LOW = "apify_cap_too_low"


@dataclass(frozen=True, slots=True)
class SocialRunConfig:
    platform: Platform
    purpose: str                      # what the run is for within a campaign: «posts», «comments», «people»…
    actor_id: str
    build: str                        # pinned build (number or tag, never «latest»): required
    max_charge_usd: float             # the actor's own cap for one run (maxTotalChargeUsd); also the estimate
    max_items: int
    build_setting: str = "APIFY_ACTOR_BUILD"   # the setting named in the error when the build is not pinned
    timeout_seconds: float = 300
    page_size: int = 100
    # A ``launching`` row younger than this is another worker's launch still in flight (its POST has not answered):
    # left alone (``busy``). Older: the launch is taken as lost and booked at its cap. Not below the POST timeout.
    launch_grace_seconds: float = 120
    max_attempts: int = 3             # launches of one input (refusals RETRYABLE included)
    # A 429 on the launch is retried within one call for at most this long without spending an attempt (0: off,
    # every 429 is an attempt), pausing rate_limit_pause_seconds between launches.
    rate_limit_seconds: float = 0
    rate_limit_pause_seconds: float = 15

    def __post_init__(self) -> None:
        actor_path(self.actor_id)
        check_pinned_build(self.build_setting, self.build)
        if not (self.purpose and len(self.purpose) <= 40 and set(self.purpose) <= _PURPOSE_CHARS
                and self.purpose[0].isalpha()):
            raise ValueError("purpose must look like posts or comments:ig")
        if not (math.isfinite(self.max_charge_usd) and 0 < self.max_charge_usd <= 5
                and 1 <= self.max_items <= 5000 and 1 <= self.page_size <= 1000 and 30 <= self.timeout_seconds <= 3600
                and math.isfinite(self.launch_grace_seconds) and 30 <= self.launch_grace_seconds <= 3600
                and 1 <= self.max_attempts <= 10
                and math.isfinite(self.rate_limit_seconds) and 0 <= self.rate_limit_seconds <= 3600
                and math.isfinite(self.rate_limit_pause_seconds) and 1 <= self.rate_limit_pause_seconds <= 300):
            raise ValueError("unsafe apify source limits")


@dataclass(frozen=True, slots=True)
class SourceRun:
    """One ``apify_source_runs`` row as the run needs it."""

    id: int
    campaign_id: str
    purpose: str
    input_hash: str
    status: str = "claimed"
    run_id: str | None = None
    dataset_id: str | None = None
    import_checkpoint: int = 0
    items_imported: int = 0
    billed_usd: float | None = None
    error_code: str | None = None
    attempts: int = 0                          # launches begun (mark_launching)
    launching_age_seconds: float | None = None  # since launched_at, by the store's clock (None: not launching)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one call did. ``retry``: try again later (the same run, never a new launch); ``busy``: another worker
    holds the launch; ``skipped``: nothing was launched (budget)."""

    status: Outcome
    run_id: str | None = None
    items_imported: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    capped: bool = False             # the run stopped on its maxTotalChargeUsd («max charge reached»)


class SourceRunStore(Protocol):
    async def claim(self, campaign_id: str, config: SocialRunConfig, input_hash: str, *,
                    owner_user_id: int | None = None) -> SourceRun: ...
    async def mark_launching(self, run: SourceRun) -> str | None: ...
    async def unmark_launching(self, run: SourceRun, token: str, *, refund: bool = False) -> None: ...
    async def save_run(self, run: SourceRun, run_id: str, dataset_id: str | None) -> None: ...
    async def checkpoint(self, run: SourceRun, *, offset: int, items: int, dataset_id: str | None = None) -> None: ...
    async def finish(self, run: SourceRun, status: str, *, billed_usd: float | None = None,
                     error: str | None = None) -> None: ...


def input_hash(actor_id: str, actor_input: dict[str, Any]) -> str:
    """The stable key of one input of one actor (the build is left out: a new build does not relaunch an input)."""
    return hashlib.sha256(json.dumps({"actor": actor_id, "input": actor_input}, sort_keys=True,
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def cap_reached(run: dict[str, Any]) -> bool:
    """The run says it stopped on its maxTotalChargeUsd: ``statusMessage`` contains «max charge reached»."""
    message = run.get("statusMessage")
    return isinstance(message, str) and CAP_REACHED in message.lower()


def _usage(run: dict[str, Any]) -> float | None:
    usage = run.get("usageTotalUsd")
    known = isinstance(usage, int | float) and not isinstance(usage, bool) and math.isfinite(usage) and usage >= 0
    return float(usage) if known else None


class ApifySocialRun:
    def __init__(self, store: SourceRunStore, http: ApifyHttp, config: SocialRunConfig, *,
                 clock: Callable[[], float] = time.monotonic, poll_seconds: int = 20,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if config.launch_grace_seconds < http.timeout_seconds:
            raise ValueError("launch_grace_seconds must not be below the Apify request timeout")
        self.store, self.http, self.config = store, http, config
        self._clock, self.poll_seconds, self._sleep = clock, poll_seconds, sleep

    def __repr__(self) -> str:
        return f"ApifySocialRun(actor={self.config.actor_id}, purpose={self.config.purpose})"

    async def run(self, campaign_id: str, actor_input: dict[str, Any], sink: Sink, *,
                  owner_user_id: int | None = None) -> RunOutcome:
        """Launch (once) or resume the run of ``actor_input`` for the campaign, and import its dataset into ``sink``."""
        cfg = self.config
        digest = input_hash(cfg.actor_id, actor_input)
        with costs.scope(campaign_id):
            row = await self.store.claim(campaign_id, cfg, digest, owner_user_id=owner_user_id)
            if row.status == "succeeded":
                return RunOutcome("succeeded", row.run_id, row.items_imported, row.billed_usd or 0.0)
            if row.status == "failed":  # paid, possibly paid, or refused for good (claim reopened what may pass)
                return RunOutcome("failed", row.run_id, row.items_imported, row.billed_usd or 0.0, row.error_code)
            try:
                return await self._run(row, actor_input, sink)
            except ApifyError as exc:
                return await self._failed(row, exc)

    async def _run(self, row: SourceRun, actor_input: dict[str, Any], sink: Sink) -> RunOutcome:
        cfg = self.config
        if row.run_id is not None and row.run_id.startswith(PENDING):
            age = row.launching_age_seconds
            if age is not None and age < cfg.launch_grace_seconds:
                return RunOutcome("busy")  # another worker's POST may still be in flight: its run is not lost
            # A launch began and its run id was never stored: a run may exist, so it is booked at its cap, not repeated.
            return await self._unconfirmed(row, row.run_id, "launch_unconfirmed")
        if row.run_id is None:
            if row.attempts >= cfg.max_attempts:
                await self.store.finish(row, "failed", error="max_attempts")
                return RunOutcome("failed", error="max_attempts")
            if await costs.over_budget(row.campaign_id):
                await costs.skip("api", "budget_cap", item=cfg.actor_id)
                return RunOutcome("skipped", error="budget_cap")
            launched = await self._launch(row, actor_input)
            if isinstance(launched, RunOutcome):
                return launched
            row = launched
        try:
            return await self._resume(row, sink)
        except ApifyError as exc:  # with the launched row: its run id is the one to resume
            return await self._failed(row, exc)

    async def _resume(self, row: SourceRun, sink: Sink) -> RunOutcome:
        """Await a launched run, bill it once and import its dataset."""
        assert row.run_id is not None
        await self._estimate(row, f"apify:{row.run_id}")  # paid from now on: booked before the wait
        run = await self._await(row.run_id)
        cost = await self._bill(row, run)
        status = str(run.get("status"))
        if status != "SUCCEEDED":
            error = "apify_run_" + status.lower().replace("-", "_")
            await self.store.finish(row, "failed", billed_usd=cost, error=error)
            log.warning("apify_source.run_failed %s", error, extra=self._extra(row))
            return RunOutcome("failed", row.run_id, row.items_imported, cost, error)
        dataset = str(run.get("defaultDatasetId") or row.dataset_id or "") or None
        try:
            imported, rows = await self._import(row, dataset, sink)
        except TemporaryApifyError:
            raise  # the row stays running: the next call resumes the import from its checkpoint
        except ApifyError as exc:
            await self.store.finish(row, "failed", billed_usd=cost, error=exc.code[:200])
            log.warning("apify_source.import_failed %s", exc.code, extra=self._extra(row))
            return RunOutcome("failed", row.run_id, row.items_imported, cost, exc.code)
        cfg, capped = self.config, cap_reached(run)
        if capped and rows == 0:  # the cap stopped the actor before its first row: not an empty search
            await self.store.finish(row, "failed", billed_usd=cost, error=CAP_TOO_LOW)
            await costs.error("api", CAP_TOO_LOW, item=cfg.actor_id)
            log.warning("apify_source.cap_too_low", extra={**self._extra(row), "max_charge_usd": cfg.max_charge_usd})
            return RunOutcome("failed", row.run_id, imported, cost, CAP_TOO_LOW, capped=True)
        await self.store.finish(row, "succeeded", billed_usd=cost)
        if capped:
            log.warning("apify_source.capped", extra={**self._extra(row), "max_charge_usd": cfg.max_charge_usd,
                                                      "rows": rows, "capped": True})
        log.info("apify_source.imported", extra={**self._extra(row), "items": imported, "capped": capped})
        return RunOutcome("succeeded", row.run_id, imported, cost, capped=capped)

    async def _launch(self, row: SourceRun, actor_input: dict[str, Any]) -> SourceRun | RunOutcome:
        cfg = self.config
        started = self._clock()
        while True:
            token = await self.store.mark_launching(row)  # before the paid launch: a failure after it never repeats it
            if token is None:  # another worker holds this launch (or it already has a run id)
                return RunOutcome("busy")
            try:
                run = await self.http.launch(cfg.actor_id, actor_input, build=cfg.build, max_items=cfg.max_items,
                                             max_charge_usd=cfg.max_charge_usd, timeout_seconds=cfg.timeout_seconds)
                break
            except LaunchUnknown:
                # A run may exist remotely: booked at its cap, and never launched a second time for this input.
                return await self._unconfirmed(replace(row, run_id=token), token, "launch_unconfirmed")
            except ApifyError as exc:  # refused outright (429, 4xx): no run exists, so no placeholder and no estimate
                pause = min(cfg.rate_limit_pause_seconds, cfg.rate_limit_seconds - (self._clock() - started))
                free = exc.code == RATE_LIMITED and pause > 0  # a 429 inside the window does not spend the attempt
                await self.store.unmark_launching(row, token, refund=free)
                if not free:
                    raise
            log.info("apify_source.rate_limited", extra=self._extra(row))
            await self._sleep(pause)
            if await costs.over_budget(row.campaign_id):  # others may have spent meanwhile
                await costs.skip("api", "budget_cap", item=cfg.actor_id)
                return RunOutcome("skipped", error="budget_cap")
        run_id, dataset = str(run["id"]), str(run.get("defaultDatasetId") or "") or None
        try:
            await self.store.save_run(row, run_id, dataset)
        except Exception:  # noqa: BLE001 - the run exists but its id is lost: never launch a second paid one
            log.warning("apify_source.run_id_not_saved", extra=self._extra(row))
            try:  # stop what it would spend; nobody can resume it without its id
                await self.http.abort(run_id)
            except Exception as exc:  # noqa: BLE001 - booked at its cap below either way
                log.warning("apify_source.abort_failed %s", getattr(exc, "code", type(exc).__name__),
                            extra=self._extra(row))
            return await self._unconfirmed(replace(row, run_id=token), token, "launch_unconfirmed")
        log.info("apify_source.launched", extra={**self._extra(row), "run_id": run_id})
        return replace(row, status="running", run_id=run_id, dataset_id=dataset)

    async def _unconfirmed(self, row: SourceRun, token: str, error: str) -> RunOutcome:
        """A launch that may have started a paid run: its cap is booked (once, key ``apify:<placeholder>``)."""
        cfg = self.config
        await self._estimate(row, f"apify:{token}")
        await costs.error("api", "apify_launch_unknown", item=cfg.actor_id)
        await self.store.finish(row, "failed", billed_usd=cfg.max_charge_usd, error=error)
        log.warning("apify_source.launch_unconfirmed", extra=self._extra(row))
        return RunOutcome("failed", row.run_id, 0, cfg.max_charge_usd, error)

    async def _estimate(self, row: SourceRun, key: str) -> None:
        await costs.record_unique("api", key=key, provider="apify", item=self.config.actor_id,
                                  cost_usd=self.config.max_charge_usd, code="estimated", campaign_id=row.campaign_id)

    async def _await(self, run_id: str) -> dict[str, Any]:
        """The run once terminal; past the timeout it is aborted (the next call resumes the same run)."""
        try:
            async with asyncio.timeout(max(1.0, self.config.timeout_seconds)):
                run = await self.http.run(run_id)
                while run.get("status") not in TERMINAL:
                    run = await self.http.run(run_id, wait_seconds=self.poll_seconds)
                return run
        except TimeoutError:
            await self.http.abort(run_id)
            raise TemporaryApifyError("apify_wait_timeout") from None

    async def _bill(self, row: SourceRun, run: dict[str, Any]) -> float:
        """The run's real cost (``usageTotalUsd``) booked once; unknown: its cap, marked ``estimated``."""
        usage = _usage(run)
        cost = usage if usage is not None else self.config.max_charge_usd
        await costs.record_unique("api", key=f"apify:{row.run_id}", provider="apify", item=self.config.actor_id,
                                  cost_usd=cost, code="" if usage is not None else "estimated",
                                  campaign_id=row.campaign_id)
        return cost

    async def _import(self, row: SourceRun, dataset: str | None, sink: Sink) -> tuple[int, int]:
        """(items the sink kept, dataset rows read) after importing from the stored checkpoint."""
        cfg = self.config
        offset, imported = row.import_checkpoint, row.items_imported
        assert row.run_id is not None
        while dataset and offset < cfg.max_items:
            limit = min(cfg.page_size, cfg.max_items - offset)
            page = await self.http.dataset_page(dataset, offset=offset, limit=limit)
            items = [scrub(item) for item in page if isinstance(item, dict)]
            if items:
                imported += max(0, int(await sink(items, row.run_id)))
            offset += len(page)
            await self.store.checkpoint(row, offset=offset, items=imported, dataset_id=dataset)
            if len(page) < limit:
                break
        return imported, offset

    async def _failed(self, row: SourceRun, exc: ApifyError) -> RunOutcome:
        cfg = self.config
        log.warning("apify_source.failed %s", exc.code, extra=self._extra(row))
        await costs.error("api", exc.code[:120], item=cfg.actor_id)
        if isinstance(exc, TemporaryApifyError):
            # A known run is resumed later; a refused launch (429) left no placeholder and is tried again later.
            return RunOutcome("retry", row.run_id, error=exc.code)
        await self.store.finish(row, "failed", error=exc.code[:200])
        return RunOutcome("failed", row.run_id, error=exc.code)

    def _extra(self, row: SourceRun) -> dict[str, Any]:
        return {"campaign_id": row.campaign_id, "actor_id": self.config.actor_id, "purpose": self.config.purpose,
                "platform": self.config.platform}


# --- PostgreSQL (migration 051) ---------------------------------------------------------------------------------

_COLUMNS = """id, campaign_id::text as campaign_id, purpose, input_hash, status, run_id, dataset_id, import_checkpoint,
              items_imported, billed_usd::float8 as billed_usd, error_code, attempts,
              case when status = 'launching' then extract(epoch from now() - launched_at)::float8 end
                  as launching_age"""


def _row(record: Any) -> SourceRun:
    return SourceRun(record["id"], record["campaign_id"], record["purpose"], record["input_hash"], record["status"],
                     record["run_id"], record["dataset_id"], record["import_checkpoint"], record["items_imported"],
                     record["billed_usd"], record["error_code"], record["attempts"], record["launching_age"])


class PostgresSourceRunStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def claim(self, campaign_id: str, config: SocialRunConfig, input_hash: str, *,
                    owner_user_id: int | None = None) -> SourceRun:
        """The row of this input (created once). A refusal that may pass later (``RETRYABLE``, nothing paid, fewer
        than ``max_attempts`` launches) is opened again; any other failure stays."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute(
                """insert into apify_source_runs (campaign_id, owner_user_id, platform, purpose, actor_id, actor_build,
                                                  input_hash, max_charge_usd)
                   values ($1::uuid, $2, $3, $4, $5, $6, $7, $8)
                   on conflict (campaign_id, purpose, input_hash) do nothing""",
                campaign_id, owner_user_id, config.platform, config.purpose, config.actor_id, config.build, input_hash,
                config.max_charge_usd)
            await conn.execute(
                """update apify_source_runs set status = 'claimed', error_code = null, finished_at = null
                    where campaign_id = $1::uuid and purpose = $2 and input_hash = $3
                      and status = 'failed' and run_id is null and error_code = any($4::text[]) and attempts < $5""",
                campaign_id, config.purpose, input_hash, sorted(RETRYABLE), config.max_attempts)
            record = await conn.fetchrow(
                f"""select {_COLUMNS} from apify_source_runs
                     where campaign_id = $1::uuid and purpose = $2 and input_hash = $3""",
                campaign_id, config.purpose, input_hash)
        return _row(record)

    async def mark_launching(self, run: SourceRun) -> str | None:
        """Before the paid launch: a ``pending:`` placeholder (returned); None when the row is not ours to launch."""
        token = f"{PENDING}{uuid.uuid4().hex}"
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            result = await conn.execute(
                """update apify_source_runs
                      set run_id = $2, status = 'launching', launched_at = now(), attempts = attempts + 1
                    where id = $1 and status = 'claimed' and run_id is null""", run.id, token)
        return token if result.split()[-1] == "1" else None

    async def unmark_launching(self, run: SourceRun, token: str, *, refund: bool = False) -> None:
        """Apify refused the launch outright (no run exists): drop our own placeholder; ``refund``: the refusal does
        not count as an attempt (a 429 inside ``rate_limit_seconds``)."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute(
                """update apify_source_runs
                      set run_id = null, status = 'claimed', launched_at = null,
                          attempts = case when $3 then greatest(attempts - 1, 0) else attempts end
                    where id = $1 and status = 'launching' and run_id = $2""", run.id, token, refund)

    async def save_run(self, run: SourceRun, run_id: str, dataset_id: str | None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            result = await conn.execute(
                """update apify_source_runs set run_id = $2, dataset_id = $3, status = 'running'
                    where id = $1 and status = 'launching' and run_id like 'pending:%'""", run.id, run_id, dataset_id)
        if result.split()[-1] != "1":
            raise RuntimeError("run_id_not_saved")

    async def checkpoint(self, run: SourceRun, *, offset: int, items: int, dataset_id: str | None = None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute(
                """update apify_source_runs
                      set import_checkpoint = greatest(import_checkpoint, $2), items_imported = $3,
                          dataset_id = coalesce(dataset_id, $4)
                    where id = $1 and status = 'running'""", run.id, offset, items, dataset_id)

    async def finish(self, run: SourceRun, status: str, *, billed_usd: float | None = None,
                     error: str | None = None) -> None:
        if status not in ("succeeded", "failed"):
            raise ValueError("a run finishes succeeded or failed")
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute(
                """update apify_source_runs
                      set status = $2, billed_usd = coalesce($3, billed_usd), error_code = $4, finished_at = now()
                    where id = $1 and status in ('claimed', 'launching', 'running')""",
                run.id, status, billed_usd, error)


async def _actor(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    await conn.execute("select set_config('app.actor', $1, true)", ACTOR)


# --- in memory (tests) -----------------------------------------------------------------------------------------


@dataclass
class MemorySourceRunStore:
    """In-memory :class:`SourceRunStore` with the same rules as the Postgres one."""

    rows: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    fail_save_run: bool = False
    clock: Callable[[], float] = time.monotonic

    def _get(self, run: SourceRun) -> dict[str, Any]:
        return self.rows[(run.campaign_id, run.purpose, run.input_hash)]

    async def claim(self, campaign_id: str, config: SocialRunConfig, input_hash: str, *,
                    owner_user_id: int | None = None) -> SourceRun:
        key = (campaign_id, config.purpose, input_hash)
        row = self.rows.setdefault(key, {
            "id": len(self.rows) + 1, "owner_user_id": owner_user_id, "platform": config.platform,
            "actor_id": config.actor_id, "actor_build": config.build, "max_charge_usd": config.max_charge_usd,
            "status": "claimed", "run_id": None, "dataset_id": None, "import_checkpoint": 0, "items_imported": 0,
            "billed_usd": None, "error_code": None, "attempts": 0, "launched_at": None})
        if (row["status"] == "failed" and row["run_id"] is None and row["error_code"] in RETRYABLE
                and row["attempts"] < config.max_attempts):
            row["status"], row["error_code"] = "claimed", None
        age = self.clock() - row["launched_at"] if row["status"] == "launching" else None
        return SourceRun(row["id"], campaign_id, config.purpose, input_hash, row["status"], row["run_id"],
                         row["dataset_id"], row["import_checkpoint"], row["items_imported"], row["billed_usd"],
                         row["error_code"], row["attempts"], age)

    async def mark_launching(self, run: SourceRun) -> str | None:
        row = self._get(run)
        if row["status"] != "claimed" or row["run_id"] is not None:
            return None
        row["run_id"] = token = f"{PENDING}{uuid.uuid4().hex}"
        row["status"], row["launched_at"], row["attempts"] = "launching", self.clock(), row["attempts"] + 1
        return token

    async def unmark_launching(self, run: SourceRun, token: str, *, refund: bool = False) -> None:
        row = self._get(run)
        if row["status"] == "launching" and row["run_id"] == token:
            row["run_id"], row["status"] = None, "claimed"
            if refund:
                row["attempts"] = max(0, row["attempts"] - 1)

    async def save_run(self, run: SourceRun, run_id: str, dataset_id: str | None) -> None:
        row = self._get(run)
        if self.fail_save_run or row["status"] != "launching":
            raise RuntimeError("run_id_not_saved")
        row.update(run_id=run_id, dataset_id=dataset_id, status="running")

    async def checkpoint(self, run: SourceRun, *, offset: int, items: int, dataset_id: str | None = None) -> None:
        row = self._get(run)
        if row["status"] == "running":
            row["import_checkpoint"] = max(row["import_checkpoint"], offset)
            row["items_imported"] = items
            row["dataset_id"] = row["dataset_id"] or dataset_id

    async def finish(self, run: SourceRun, status: str, *, billed_usd: float | None = None,
                     error: str | None = None) -> None:
        if status not in ("succeeded", "failed"):
            raise ValueError("a run finishes succeeded or failed")
        row = self._get(run)
        if row["status"] in ("claimed", "launching", "running"):
            row["status"], row["error_code"] = status, error
            if billed_usd is not None:
                row["billed_usd"] = billed_usd


__all__ = ["CAP_REACHED", "CAP_TOO_LOW", "RATE_LIMITED", "RETRYABLE", "ApifySocialRun", "MemorySourceRunStore", "PostgresSourceRunStore", "RunOutcome", "SocialRunConfig",
           "SourceRun", "SourceRunStore", "input_hash"]
