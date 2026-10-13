"""The Apify acquisition provider for Facebook batches (``FEATURE_APIFY_FACEBOOK``, off by default).

It executes the same queued batch the browser collector would (``provider.AcquisitionProvider``): one actor run
for the batch's public groups, then the run's dataset becomes ``collected_posts`` rows (``AcquiredPost``), so the
analysis worker, the campaign's rules, dedup and report treat them like every other post. Public groups only: the
actor gets no cookies and no account, and nothing here joins a group or gets past a login.

Order of a batch, every step resumable after a crash:

1. the run is looked up (``store.get_run``); a run that exists is never launched again;
2. a new launch first checks the campaign budget (``CAMPAIGN_BUDGET_USD``) and this provider's own cap
   (``max_usd_per_campaign``); the launch is one POST, never retried -- a lost answer is stored as
   ``LAUNCH_UNKNOWN`` with the cap booked as an estimate, and the batch fails without a second paid run;
3. the run id is stored before anything else is done with it; then the run is awaited (bounded, aborted on timeout),
   its real cost booked once (``costs.record_unique``, key ``apify:<run_id>``);
4. the dataset is imported page by page from the stored checkpoint (every ``checkpoint_every`` items the offset is
   saved; a raw item already stored is skipped), each item mapped or counted as rejected;
5. each group ends ``ACTIVE`` (posts, or a successful run that found none: ``no_posts``) or ``INACCESSIBLE`` (the
   actor reported it closed or gone) -- an empty run is not mistaken for an unreachable group.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from bot.acquisition.apify_client import (
    TERMINAL,
    ApifyError,
    ApifyHttp,
    LaunchUnknown,
    PermanentApifyError,
    TemporaryApifyError,
    actor_path,
)
from bot.utils import costs

from ..models import BatchItem, BatchPlan, GroupState
from ..provider import ProviderHealth
from .mapper import apify_item_to_post, group_error, group_of
from .metrics import emit

log = logging.getLogger(__name__)
LAUNCH_UNKNOWN = "LAUNCH_UNKNOWN"
FALLBACK = "apify_fallback"  # acquisition_batches.requested_by prefix of a closed groups' browser batch
DEFAULT_ACTOR = "apify/facebook-groups-scraper"


@dataclass(frozen=True, slots=True)
class ApifyFacebookConfig:
    actor_id: str = DEFAULT_ACTOR
    build: str = ""                      # pinned build (never «latest» in production); required to enable
    max_posts_per_run: int = 500
    max_usd_per_campaign: float = 10.0   # this provider's spend per campaign, on top of CAMPAIGN_BUDGET_USD
    max_usd_per_run: float = 2.0         # the actor's own charge cap for one run (maxTotalChargeUsd)
    timeout_seconds: float = 600
    page_size: int = 200
    checkpoint_every: int = 50
    # Closed groups the actor could not read go to the browser collector as their own batch (once per batch).
    browser_fallback: bool = True

    def __post_init__(self) -> None:
        actor_path(self.actor_id)
        if not self.build or self.build.lower() == "latest":
            raise ValueError("APIFY_FACEBOOK_ACTOR_BUILD must name a pinned build")
        if not (1 <= self.max_posts_per_run <= 5000 and 1 <= self.page_size <= 1000 and self.checkpoint_every >= 1
                and math.isfinite(self.max_usd_per_campaign) and self.max_usd_per_campaign > 0
                and math.isfinite(self.max_usd_per_run) and 0 < self.max_usd_per_run <= self.max_usd_per_campaign
                and 30 <= self.timeout_seconds <= 3600):
            raise ValueError("unsafe apify facebook limits")


@dataclass(frozen=True, slots=True)
class ApifyRun:
    batch_id: str
    run_id: str
    status: str
    dataset_id: str | None = None
    import_checkpoint: int = 0
    items_imported: int = 0


class ApifyFacebookStore(Protocol):
    async def load_plan(self, batch_id: str) -> BatchPlan: ...
    async def campaign_of(self, batch_id: str) -> str | None: ...
    async def start(self, plan: BatchPlan) -> str: ...
    async def start_item(self, item: BatchItem, batch_run_id: str) -> str: ...
    async def get_run(self, batch_id: str) -> ApifyRun | None: ...
    async def save_run(self, batch_id: str, campaign_id: str | None, *, actor_id: str, build: str, run_id: str,
                       status: str, input_hash: str, dataset_id: str | None = None) -> None: ...
    async def update_run(self, run_id: str, **fields: Any) -> None: ...
    async def spent(self, campaign_id: str) -> float: ...
    async def save_raw(self, run_id: str, index: int, payload: dict[str, Any]) -> bool: ...
    async def save_post(self, source_id: str, acquisition_run_id: str, row: dict[str, Any]) -> None: ...
    async def finish_item(self, item: BatchItem, run_id: str, state: str, group_state: GroupState,
                          detail: str | None = None) -> None: ...
    async def finish_batch(self, plan: BatchPlan, batch_run_id: str, state: str, reason: str | None = None) -> None: ...
    async def queue_browser_fallback(self, plan: BatchPlan, items: list[BatchItem],
                                     campaign_id: str | None) -> str | None: ...


_SECRET_KEY = re.compile(r"cookie|session|token|password|secret|auth", re.IGNORECASE)


def scrub(value: Any) -> Any:
    """The raw item without any key that could carry session data (cookies, tokens): never stored, never logged."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _norm(url: str | None) -> str:
    return (url or "").lower().split("?", 1)[0].rstrip("/").replace("://m.", "://www.").replace("://facebook.", "://www.facebook.")


class ApifyFacebookProvider:
    name = "apify"

    def __init__(self, store: ApifyFacebookStore, http: ApifyHttp, config: ApifyFacebookConfig) -> None:
        self.store, self.http, self.config = store, http, config

    def __repr__(self) -> str:
        return f"ApifyFacebookProvider(actor={self.config.actor_id})"

    async def aclose(self) -> None:
        await self.http.aclose()

    async def health(self) -> ProviderHealth:
        """The token works and the actor exists (one GET; nothing is launched)."""
        try:
            await self.http.data("GET", f"acts/{actor_path(self.config.actor_id)}")
        except ApifyError as exc:
            return ProviderHealth(self.name, False, exc.code)
        return ProviderHealth(self.name, True)

    async def run(self, batch_id: str) -> str:
        plan = await self.store.load_plan(batch_id)
        campaign_id = await self.store.campaign_of(batch_id)
        batch_run = await self.store.start(plan)
        runs = {item.id: await self.store.start_item(item, batch_run) for item in plan.items}
        with costs.scope(campaign_id):
            try:
                return await self._run(plan, campaign_id, batch_run, runs)
            except ApifyError as exc:
                log.warning("apify.run_failed %s", exc.code, extra={"batch_id": batch_id, "campaign_id": campaign_id})
                emit("apify_run_failed_total", actor_id=self.config.actor_id, reason=exc.code, campaign_id=campaign_id,
                     batch_id=batch_id)
                await costs.error("api", exc.code, item=self.config.actor_id)
                return await self._end(plan, batch_run, runs, "failed", exc.code)

    async def _run(self, plan: BatchPlan, campaign_id: str | None, batch_run: str, runs: dict[str, str]) -> str:
        cfg, started = self.config, time.monotonic()
        known = await self.store.get_run(plan.id)
        if known is not None and known.status == LAUNCH_UNKNOWN:
            emit("apify_run_failed_total", actor_id=cfg.actor_id, reason="apify_launch_unknown", campaign_id=campaign_id,
                 batch_id=plan.id, run_id=known.run_id)
            return await self._end(plan, batch_run, runs, "failed", "apify_launch_unknown")
        if known is None:
            refusal = await self._budget(campaign_id)
            if refusal:
                emit("apify_run_failed_total", actor_id=cfg.actor_id, reason=refusal, campaign_id=campaign_id,
                     batch_id=plan.id)
                await costs.skip("api", refusal, item=cfg.actor_id)
                return await self._end(plan, batch_run, runs, "failed", refusal)
            known = await self._launch(plan, campaign_id)
            if known is None:
                return await self._end(plan, batch_run, runs, "failed", "apify_launch_unknown")
        run = await self._await(known.run_id, started)
        await self._bill(campaign_id, known.run_id, run)
        status = str(run.get("status"))
        if status != "SUCCEEDED":
            emit("apify_run_failed_total", actor_id=cfg.actor_id, reason="apify_run_" + status.lower().replace("-", "_"),
                 campaign_id=campaign_id, batch_id=plan.id, run_id=known.run_id)
            await self.store.update_run(known.run_id, status=status.replace("-", "_"))
            return await self._end(plan, batch_run, runs, "failed", "apify_run_" + status.lower().replace("-", "_"))
        dataset = str(run.get("defaultDatasetId") or known.dataset_id or "")
        if dataset != (known.dataset_id or ""):
            await self.store.update_run(known.run_id, dataset_id=dataset)
        posts, errors = await self._import(plan, campaign_id, known, dataset, runs)
        for item in plan.items:
            code = errors.get(_norm(item.canonical_url))
            if code:
                await self.store.finish_item(item, runs[item.id], "failed", GroupState.INACCESSIBLE, code)
            else:
                await self.store.finish_item(item, runs[item.id], "succeeded", GroupState.ACTIVE,
                                             None if posts[item.id] else "no_posts")
        await self.store.finish_batch(plan, batch_run, "succeeded")
        closed = [item for item in plan.items if errors.get(_norm(item.canonical_url)) == "group_private"]
        if closed and cfg.browser_fallback:
            # Public groups only through Apify: a closed group is read by the operator's browser profile, where
            # the account already is a member -- nothing joins a group.
            fallback = await self.store.queue_browser_fallback(plan, closed, campaign_id)
            log.info("apify.browser_fallback", extra={"batch_id": plan.id, "campaign_id": campaign_id,
                                                      "fallback_batch_id": fallback, "groups": len(closed)})
        return "succeeded"

    async def _budget(self, campaign_id: str | None) -> str | None:
        if campaign_id and await costs.over_budget(campaign_id):
            return "budget_cap"
        if campaign_id and await self.store.spent(campaign_id) + self.config.max_usd_per_run > self.config.max_usd_per_campaign:
            return "apify_budget_cap"
        return None

    async def _launch(self, plan: BatchPlan, campaign_id: str | None) -> ApifyRun | None:
        cfg = self.config
        actor_input = {"startUrls": [{"url": item.canonical_url} for item in plan.items],
                       "resultsLimit": cfg.max_posts_per_run, "viewOption": "CHRONOLOGICAL"}
        digest = hashlib.sha256(json.dumps(actor_input, sort_keys=True).encode()).hexdigest()
        try:
            run = await self.http.launch(cfg.actor_id, actor_input, build=cfg.build, max_items=cfg.max_posts_per_run,
                                         max_charge_usd=cfg.max_usd_per_run, timeout_seconds=cfg.timeout_seconds)
        except LaunchUnknown:
            # A run may exist remotely: booked at its cap, recorded, and never launched a second time for this batch.
            await self.store.save_run(plan.id, campaign_id, actor_id=cfg.actor_id, build=cfg.build,
                                      run_id=f"unknown-{plan.id}", status=LAUNCH_UNKNOWN, input_hash=digest)
            await costs.record_unique("api", key=f"apify-launch-unknown:{plan.id}", provider="apify",
                                      item=cfg.actor_id, cost_usd=cfg.max_usd_per_run, code="estimate")
            await costs.error("api", "apify_launch_unknown", item=cfg.actor_id)
            log.warning("apify.launch_unknown", extra={"batch_id": plan.id, "campaign_id": campaign_id})
            emit("apify_run_failed_total", actor_id=cfg.actor_id, reason="apify_launch_unknown", campaign_id=campaign_id,
                 batch_id=plan.id)
            return None
        run_id, dataset = str(run["id"]), str(run.get("defaultDatasetId") or "") or None
        await self.store.save_run(plan.id, campaign_id, actor_id=cfg.actor_id, build=cfg.build, run_id=run_id,
                                  status="RUNNING", input_hash=digest, dataset_id=dataset)
        emit("apify_run_launched_total", actor_id=cfg.actor_id, campaign_id=campaign_id, batch_id=plan.id, run_id=run_id)
        return ApifyRun(plan.id, run_id, "RUNNING", dataset)

    async def _await(self, run_id: str, started: float) -> dict[str, Any]:
        """The run once terminal; past the timeout it is aborted (the next attempt resumes the same run)."""
        left = self.config.timeout_seconds - (time.monotonic() - started)
        try:
            async with asyncio.timeout(max(1.0, left)):
                run = await self.http.run(run_id)
                while run.get("status") not in TERMINAL:
                    run = await self.http.run(run_id, wait_seconds=20)
                return run
        except TimeoutError:
            await self.http.abort(run_id)
            raise TemporaryApifyError("apify_wait_timeout") from None

    async def _bill(self, campaign_id: str | None, run_id: str, run: dict[str, Any]) -> None:
        usage = run.get("usageTotalUsd")
        known = isinstance(usage, int | float) and not isinstance(usage, bool) and math.isfinite(usage) and usage >= 0
        cost = float(usage) if known else self.config.max_usd_per_run
        await costs.record_unique("api", key=f"apify:{run_id}", provider="apify", item=self.config.actor_id,
                                  cost_usd=cost, code="" if known else "estimate", campaign_id=campaign_id)
        await self.store.update_run(run_id, billed_usd=cost)
        emit("apify_billed_usd_total", cost, actor_id=self.config.actor_id, campaign_id=campaign_id, run_id=run_id,
             estimate=None if known else "true")

    async def _import(self, plan: BatchPlan, campaign_id: str | None, run: ApifyRun, dataset: str,
                      runs: dict[str, str]) -> tuple[Counter[str], dict[str, str]]:
        cfg, started = self.config, time.monotonic()
        by_group = {_norm(item.canonical_url): item for item in plan.items}
        posts: Counter[str] = Counter()
        errors: dict[str, str] = {}
        rejected: Counter[str] = Counter()
        offset, imported = run.import_checkpoint, run.items_imported
        if offset:
            emit("apify_checkpoint_resumed_total", actor_id=cfg.actor_id, campaign_id=campaign_id, run_id=run.run_id)
        since_checkpoint = 0
        while imported < cfg.max_posts_per_run:
            page = await self.http.dataset_page(dataset, offset=offset, limit=cfg.page_size) if dataset else []
            for index, raw in enumerate(page, start=offset):
                raw = scrub(raw)
                if not isinstance(raw, dict) or not await self.store.save_raw(run.run_id, index, raw):
                    continue
                failed = group_error(raw)
                if failed is not None:
                    errors[_norm(failed[0])] = failed[1]
                    emit("apify_items_rejected_total", actor_id=cfg.actor_id, reason=failed[1], campaign_id=campaign_id,
                         run_id=run.run_id, source_url=failed[0])
                    continue
                item = by_group.get(_norm(group_of(raw))) or (plan.items[0] if len(plan.items) == 1 else None)
                if item is None:
                    rejected["apify_unknown_group"] += 1
                    continue
                try:
                    post = apify_item_to_post(raw, campaign_id=campaign_id or "", actor_id=cfg.actor_id,
                                              run_id=run.run_id, group_url=item.canonical_url)
                except (ValueError, ValidationError):
                    rejected["apify_invalid_item"] += 1
                    continue
                await self.store.save_post(item.source_id, runs[item.id], post.collected_post_row())
                posts[item.id] += 1
                imported += 1
                since_checkpoint += 1
                if since_checkpoint >= cfg.checkpoint_every:
                    await self.store.update_run(run.run_id, import_checkpoint=index + 1, items_imported=imported)
                    since_checkpoint = 0
                if imported >= cfg.max_posts_per_run:
                    break
            offset += len(page)
            if len(page) < cfg.page_size:
                break
        await self.store.update_run(run.run_id, status="SUCCEEDED", import_checkpoint=offset, items_imported=imported)
        for code, n in rejected.items():
            await costs.record("api", kind="skip", provider="apify", item=cfg.actor_id, code=code, units=n)
            emit("apify_items_rejected_total", n, actor_id=cfg.actor_id, reason=code, campaign_id=campaign_id,
                 run_id=run.run_id)
        emit("apify_items_imported_total", sum(posts.values()), actor_id=cfg.actor_id, campaign_id=campaign_id,
             run_id=run.run_id)
        emit("apify_import_duration_seconds", time.monotonic() - started, actor_id=cfg.actor_id,
             campaign_id=campaign_id, run_id=run.run_id)
        return posts, errors

    async def _end(self, plan: BatchPlan, batch_run: str, runs: dict[str, str], state: str, reason: str) -> str:
        for item in plan.items:
            await self.store.finish_item(item, runs[item.id], state, GroupState.UNKNOWN, reason)
        await self.store.finish_batch(plan, batch_run, state, reason)
        return state


__all__ = ["DEFAULT_ACTOR", "LAUNCH_UNKNOWN", "ApifyFacebookConfig", "ApifyFacebookProvider", "ApifyFacebookStore",
           "ApifyRun", "PermanentApifyError", "TemporaryApifyError"]
