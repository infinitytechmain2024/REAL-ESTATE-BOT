"""What a campaign run costs: a ledger of paid calls by stage, a budget, and what failed or was skipped on the way.

Every paid call -- an LLM answer from OpenRouter, a page from the scrape API, a paid search -- is one row of
``campaign_costs`` (migration 042): the campaign it was made for, the stage (``search``, ``fetch``, ``scrape``,
``api``, ``llm``), what was used (a model or a site) and the price in USD. The same table keeps the things a report
must not hide: an LLM call that failed (``kind='error'``, its code) and a page that was dropped by a cheap filter
before any model saw it (``kind='skip'``, its reason).

The campaign comes from a context variable (``scope``), so the LLM clients need no new parameter: the web worker,
the runner and the analysis worker open a scope around the work they do for one campaign. Calls outside any scope
(Facebook monitoring, the Telegram interview) are recorded without a campaign.

The price of an LLM call is OpenRouter's own ``usage.cost`` (asked for with ``"usage": {"include": true}``); when an
answer has none, it is estimated from the token counts with ``PRICES``. A process that never called ``install`` (unit
tests, one-off scripts) records nothing and never hits a budget. The ledger is bookkeeping: a failed write is logged
and the work goes on.

``CAMPAIGN_BUDGET_USD`` (0: no limit) is the most one campaign may spend. The web stage stops (``budget_cap``), the
analysis worker skips the campaign's posts and the AI check holds findings once ``over_budget`` says so; every
service reads the same total from the database, so the limit holds across processes.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import asyncpg

log = logging.getLogger(__name__)

STAGES = ("search", "fetch", "scrape", "api", "llm")
KINDS = ("cost", "error", "skip")
# USD per million tokens (input, output) when OpenRouter's answer carries no ``usage.cost``; a model is matched by
# the longest key it starts with. Deliberately on the high side: an estimate must not let a run overspend.
PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-opus": (15.0, 75.0),
    "anthropic/claude-sonnet": (3.0, 15.0),
    "anthropic/claude-haiku": (1.0, 5.0),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.5, 10.0),
    "openai/whisper": (0.0, 0.0),
}
DEFAULT_PRICE = (3.0, 15.0)


@dataclass(frozen=True, slots=True)
class Entry:
    """One ledger row. ``item``: the model or the site; ``code``: an error code or a skip reason."""

    campaign_id: str | None
    stage: str
    kind: str = "cost"
    provider: str = ""
    item: str = ""
    code: str = ""
    units: int = 1
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class CostSummary:
    """A campaign's spend: USD per stage and per item (model or site), errors and skips by code."""

    by_stage: dict[str, float] = field(default_factory=dict)
    by_item: dict[str, float] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)   # "stage:code" -> count
    skips: dict[str, int] = field(default_factory=dict)    # "stage:code" -> count
    estimates: dict[str, float] = field(default_factory=dict)  # stage -> unconfirmed USD already included in by_stage

    @property
    def total(self) -> float:
        return sum(self.by_stage.values())


class Ledger(Protocol):
    async def add(self, entry: Entry) -> None: ...
    async def upsert(self, entry: Entry, key: str) -> None: ...
    async def spent(self, campaign_id: str) -> float: ...
    async def summary(self, campaign_id: str) -> CostSummary: ...


def _summary(entries: list[Entry]) -> CostSummary:
    stages: Counter[str] = Counter()
    items: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    skips: Counter[str] = Counter()
    estimates: Counter[str] = Counter()
    for e in entries:
        if e.kind == "cost":
            stages[e.stage] += e.cost_usd
            if e.item:
                items[e.item] += e.cost_usd
            if e.code.startswith("estimated"):
                estimates[e.stage] += e.cost_usd
        elif e.kind == "error":
            errors[f"{e.stage}:{e.code}"] += e.units
        else:
            skips[f"{e.stage}:{e.code}"] += e.units
    return CostSummary(dict(stages), dict(items), dict(errors), dict(skips), dict(estimates))


@dataclass
class MemoryLedger:
    """In-process twin of ``PostgresLedger`` (tests)."""

    entries: list[Entry] = field(default_factory=list)
    keys: dict[str, int] = field(default_factory=dict)

    async def add(self, entry: Entry) -> None:
        self.entries.append(entry)

    async def upsert(self, entry: Entry, key: str) -> None:
        if key in self.keys:
            previous = self.entries[self.keys[key]]
            if entry.code.startswith("estimated") and not previous.code.startswith("estimated"):
                return
            self.entries[self.keys[key]] = entry
        else:
            self.keys[key] = len(self.entries)
            self.entries.append(entry)

    async def spent(self, campaign_id: str) -> float:
        return sum(e.cost_usd for e in self.entries if e.campaign_id == campaign_id and e.kind == "cost")

    async def summary(self, campaign_id: str) -> CostSummary:
        return _summary([e for e in self.entries if e.campaign_id == campaign_id])


class PostgresLedger:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def add(self, entry: Entry) -> None:
        await self.pool.execute(
            """insert into campaign_costs (campaign_id, stage, kind, provider, item, code, units, cost_usd)
               values ($1::uuid, $2, $3, nullif($4, ''), nullif($5, ''), nullif($6, ''), $7, $8)""",
            entry.campaign_id, entry.stage, entry.kind, entry.provider[:40], entry.item[:120], entry.code[:80],
            max(0, entry.units), max(0.0, entry.cost_usd))

    async def upsert(self, entry: Entry, key: str) -> None:
        await self.pool.execute(
            """insert into campaign_costs
               (campaign_id, stage, kind, provider, item, code, units, cost_usd, idempotency_key)
               values ($1::uuid,$2,$3,nullif($4,''),nullif($5,''),nullif($6,''),$7,$8,$9)
               on conflict (idempotency_key) do update set
               campaign_id=excluded.campaign_id, stage=excluded.stage, kind=excluded.kind,
               provider=excluded.provider, item=excluded.item, code=excluded.code,
               units=excluded.units, cost_usd=excluded.cost_usd
               where coalesce(campaign_costs.code, '') like 'estimated%'
                  or coalesce(excluded.code, '') not like 'estimated%'""",
            entry.campaign_id, entry.stage, entry.kind, entry.provider[:40], entry.item[:120], entry.code[:80],
            max(0, entry.units), max(0.0, entry.cost_usd), key)

    async def spent(self, campaign_id: str) -> float:
        value = await self.pool.fetchval(
            "select coalesce(sum(cost_usd), 0) from campaign_costs where campaign_id = $1::uuid and kind = 'cost'",
            campaign_id)
        return float(value or 0)

    async def summary(self, campaign_id: str) -> CostSummary:
        rows = await self.pool.fetch(
            """select stage, kind, coalesce(item, '') as item, coalesce(code, '') as code,
                      sum(units)::int as units, sum(cost_usd)::float8 as cost
                 from campaign_costs where campaign_id = $1::uuid group by 1, 2, 3, 4""", campaign_id)
        return _summary([Entry(campaign_id, r["stage"], r["kind"], "", r["item"], r["code"], r["units"], r["cost"])
                         for r in rows])


# --- the process-wide sink and the current campaign ------------------------------------------------------------------

_campaign: ContextVar[str | None] = ContextVar("costs_campaign", default=None)
_state: dict[str, Any] = {"ledger": None, "budget": 0.0}


def install(ledger: Ledger | None, budget_usd: float = 0.0) -> None:
    """Set the ledger (None: record nothing) and the per-campaign budget (0: no limit) of this process."""
    _state["ledger"], _state["budget"] = ledger, max(0.0, float(budget_usd or 0))


def ledger() -> Ledger | None:
    ledger_: Ledger | None = _state["ledger"]
    return ledger_


def budget() -> float:
    return float(_state["budget"])


@contextmanager
def scope(campaign_id: str | None) -> Iterator[None]:
    """Everything recorded inside is booked on ``campaign_id``."""
    token = _campaign.set(campaign_id)
    try:
        yield
    finally:
        _campaign.reset(token)


def current() -> str | None:
    return _campaign.get()


async def record(stage: str, *, kind: str = "cost", provider: str = "", item: str = "", code: str = "",
                 units: int = 1, cost_usd: float = 0.0, campaign_id: str | None = None) -> None:
    sink = ledger()
    if sink is None:
        return
    if stage not in STAGES or kind not in KINDS:
        raise ValueError(f"unknown cost stage/kind {stage}/{kind}")
    entry = Entry(campaign_id or current(), stage, kind, provider, item, code, units,
                  cost_usd if math.isfinite(cost_usd) else 0.0)
    try:
        await sink.add(entry)
    except Exception:  # noqa: BLE001 - bookkeeping must never stop the work it books
        log.warning("costs.record_failed", extra={"stage": stage, "kind": kind})


async def record_unique(stage: str, *, key: str, provider: str, item: str, cost_usd: float, code: str = "",
                        units: int = 1, campaign_id: str | None = None) -> None:
    """Upsert one run's globally stable key; estimates never replace known actual usage."""
    sink = ledger()
    if sink is None:
        return
    if stage not in STAGES or not key:
        raise ValueError("unknown cost stage or empty idempotency key")
    entry = Entry(campaign_id or current(), stage, "cost", provider, item, code, units,
                  max(0.0, cost_usd) if math.isfinite(cost_usd) else 0.0)
    try:
        await sink.upsert(entry, key)
    except Exception:  # noqa: BLE001 - bookkeeping must never stop the work it books
        log.warning("costs.record_unique_failed", extra={"stage": stage})


async def error(stage: str, code: str, *, item: str = "", campaign_id: str | None = None) -> None:
    await record(stage, kind="error", code=code, item=item, campaign_id=campaign_id)


async def skip(stage: str, code: str, *, item: str = "", campaign_id: str | None = None) -> None:
    await record(stage, kind="skip", code=code, item=item, campaign_id=campaign_id)


def price(model: str) -> tuple[float, float]:
    best = max((k for k in PRICES if model.startswith(k)), key=len, default=None)
    return PRICES[best] if best else DEFAULT_PRICE


def llm_cost(model: str, body: Any) -> tuple[float, int]:
    """(USD, tokens) of one chat completion answer: ``usage.cost`` when OpenRouter sent it, else an estimate."""
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    prompt, completion = _int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens"))
    cost = usage.get("cost")
    if isinstance(cost, int | float) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
        return float(cost), prompt + completion
    pin, pout = price(model)
    return (prompt * pin + completion * pout) / 1_000_000, prompt + completion


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


async def llm(model: str, body: Any, *, provider: str = "openrouter") -> None:
    """Book one LLM answer (``body``: the parsed JSON response)."""
    if ledger() is None:
        return
    cost, tokens = llm_cost(model, body)
    await record("llm", provider=provider, item=model, units=max(1, tokens), cost_usd=cost)


USAGE = {"include": True}  # the request field that makes OpenRouter return ``usage.cost``


async def over_budget(campaign_id: str | None = None) -> bool:
    """True when the campaign (default: the current one) has spent its ``CAMPAIGN_BUDGET_USD``."""
    sink, limit, cid = ledger(), budget(), campaign_id or current()
    if sink is None or limit <= 0 or not cid:
        return False
    try:
        return await sink.spent(cid) >= limit
    except Exception:  # noqa: BLE001 - an unreadable ledger never stops a campaign
        log.warning("costs.spent_failed")
        return False


async def llm_response(model: str, response: Any) -> None:
    """Book an ``httpx.Response`` of a chat completion (an unreadable body costs the estimate of nothing)."""
    if ledger() is None:
        return
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - the caller reports the unreadable answer itself
        body = None
    await llm(model, body)
