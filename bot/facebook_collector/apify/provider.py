"""Durable Apify Facebook provider, wired by the campaign runner in T6."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from bot.acquisition.evidence import Evidence
from bot.facebook_collector.provider import ProviderHealth

from .client import ApifyClient, RunStatus
from .mapper import apify_item_to_evidence

TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})


@dataclass(frozen=True, slots=True)
class ApifyFacebookConfig:
    actor_id: str
    actor_build: str
    max_posts_per_run: int = 500
    timeout_seconds: float = 600
    checkpoint_every: int = 100
    page_size: int = 1000
    max_usd_per_run: float = 10

    def __post_init__(self) -> None:
        if not self.actor_id or not self.actor_build:
            raise ValueError("actor_id and actor_build are required")
        if self.max_posts_per_run <= 0 or self.checkpoint_every <= 0:
            raise ValueError("invalid Apify provider limits")
        if not 1 <= self.page_size <= 1000:
            raise ValueError("invalid Apify dataset page size")
        if self.timeout_seconds <= 0 or not math.isfinite(self.max_usd_per_run) or self.max_usd_per_run <= 0:
            raise ValueError("invalid Apify provider budget")


@dataclass(frozen=True, slots=True)
class ApifyBatch:
    batch_id: str
    campaign_id: str
    group_urls: tuple[str, ...]
    run_id: str | None = None
    dataset_id: str | None = None
    checkpoint: int = 0


class ApifyRunStore(Protocol):
    async def load_plan(self, batch_id: str) -> ApifyBatch: ...
    async def save_run(self, batch_id: str, run_id: str, dataset_id: str | None, input_hash: str) -> None: ...
    async def save_launch_unknown(self, batch_id: str, input_hash: str) -> None: ...
    async def save_item(self, run_id: str, item_index: int, raw_item: dict[str, Any], evidence: Evidence) -> None: ...
    async def checkpoint(self, run_id: str, offset: int) -> None: ...
    async def finish(self, run_id: str, status: str, item_count: int, billed_usd: float | None, detail: str | None = None) -> None: ...


class ApifyFacebookProvider:
    provider = "apify_facebook"

    def __init__(
        self,
        store: ApifyRunStore,
        client: ApifyClient,
        config: ApifyFacebookConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_seconds: float = 2,
    ) -> None:
        self._store = store
        self._client = client
        self._config = config
        self._sleep = sleep
        self._poll_seconds = poll_seconds

    async def health(self) -> ProviderHealth:
        if not self._client.configured:
            return ProviderHealth(self.provider, False, "apify_token_missing")
        return ProviderHealth(self.provider, True)

    async def run(self, batch_id: str) -> str:
        plan = await self._store.load_plan(batch_id)
        actor_input = {
            "startUrls": [{"url": url} for url in plan.group_urls],
            "resultsLimit": self._config.max_posts_per_run,
            "viewOption": "CHRONOLOGICAL",
        }
        input_hash = hashlib.sha256(
            json.dumps(actor_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        run_id, dataset_id = plan.run_id, plan.dataset_id
        if run_id is None:
            handle = await self._client.run_actor(
                self._config.actor_id,
                self._config.actor_build,
                actor_input,
                {
                    "timeout": self._config.timeout_seconds,
                    "maxTotalChargeUsd": self._config.max_usd_per_run,
                    "restartOnError": "false",
                },
            )
            if handle.status == "LAUNCH_UNKNOWN" or handle.run_id is None:
                await self._store.save_launch_unknown(batch_id, input_hash)
                return "launch_unknown"
            run_id, dataset_id = handle.run_id, handle.dataset_id
            await self._store.save_run(batch_id, run_id, dataset_id, input_hash)

        status = await self._wait_for_terminal(run_id)
        dataset_id = status.dataset_id or dataset_id
        if status.status != "SUCCEEDED":
            result = "group_inaccessible" if _group_inaccessible(status.status_message) else "failed"
            await self._store.finish(run_id, result, plan.checkpoint, status.billed_usd, status.status_message)
            return result
        if dataset_id is None:
            await self._store.finish(run_id, "failed", plan.checkpoint, status.billed_usd, "dataset_missing")
            return "failed"

        imported = plan.checkpoint
        async for item in self._client.iter_dataset_items(
            dataset_id, offset=plan.checkpoint, page_size=self._config.page_size
        ):
            group_url = _item_group_url(item, plan.group_urls)
            evidence = apify_item_to_evidence(
                item,
                campaign_id=plan.campaign_id,
                actor_id=self._config.actor_id,
                run_id=run_id,
                group_url=group_url,
            )
            await self._store.save_item(run_id, imported, item, evidence)
            imported += 1
            if imported % self._config.checkpoint_every == 0:
                await self._store.checkpoint(run_id, imported)
        if imported != plan.checkpoint:
            await self._store.checkpoint(run_id, imported)
        result = "succeeded_empty" if imported == plan.checkpoint == 0 else "succeeded"
        await self._store.finish(run_id, result, imported, status.billed_usd)
        return result

    async def _wait_for_terminal(self, run_id: str) -> RunStatus:
        elapsed = 0.0
        while True:
            status = await self._client.get_run_status(run_id)
            if status.status in TERMINAL_STATUSES:
                return status
            if elapsed >= self._config.timeout_seconds:
                return RunStatus(run_id, status.dataset_id, "TIMED-OUT", status.billed_usd, "provider_timeout")
            await self._sleep(self._poll_seconds)
            elapsed += self._poll_seconds


def _item_group_url(item: dict[str, Any], configured: tuple[str, ...]) -> str:
    value = item.get("groupUrl")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return configured[0] if len(configured) == 1 else "unknown"


def _group_inaccessible(message: str | None) -> bool:
    normalized = (message or "").casefold()
    return any(marker in normalized for marker in ("inaccessible", "not accessible", "unavailable", "private group"))
