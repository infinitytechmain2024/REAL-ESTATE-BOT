from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from bot.utils import costs

from .formatters import digest
from .models import Evidence
from .openrouter import OpenRouterAnalyzer
from .pipeline import AnalysisPipeline
from .prefilter import TaskContext, prefilter
from .settings import AnalysisSettings
from .store import PostgresAnalysisStore
from .telegram import send_digest

log = logging.getLogger(__name__)
Sender = Callable[[int, str], Awaitable[int]]
# Telegram's hard limit is 4096; a card is built to fit, this is the last guard.
MAX_MESSAGE_CHARS = 3900


def one_per_message(entries: list[tuple[str, str]], limit: int = MAX_MESSAGE_CHARS) -> list[list[tuple[str, str]]]:
    """Each finding is its own Telegram message: one card, one message."""
    return [[(fid, text[:limit])] for fid, text in entries]


async def deliver(store, chat_id: int, send: Sender) -> int:
    """Send every recorded, undelivered digest; a failure leaves it queued for the next cycle."""
    sent = 0
    for digest_id, body in await store.unsent_digests(chat_id):
        try:
            message_id = await send(chat_id, body)
        except (httpx.HTTPError, ValueError):
            log.warning("analysis.digest_delivery_failed", extra={"digest_id": digest_id})
            break
        await store.mark_digest_sent(digest_id, message_id)
        sent += 1
    return sent


async def analyse_batch(store, pipeline: AnalysisPipeline, *, batch_size: int, claim_seconds: int, model: str) -> tuple[dict[str, list[tuple[str, str]]], int]:
    """Analyse one claimed batch; returns findings per vertical and how many posts were claimed.

    A campaign post goes through the campaign's budget (``CAMPAIGN_BUDGET_USD``) and the cheap ``prefilter`` first;
    a post either of them drops is booked in the cost ledger (``skip``) and closed without a model call. A model
    answer that can never be used (outside the schema, a refused request) closes the post and is booked as an
    ``error`` with its code, so the campaign's report says what happened; a key or billing refusal (401/402) gives
    the post back and stops the batch instead of trying every post with a dead key.
    """
    formatted: dict[str, list[tuple[str, str]]] = {"real_estate": [], "investors": []}
    rows = await store.pending(batch_size, claim_seconds=claim_seconds)
    for index, row in enumerate(rows):
        token = str(row["analysis_claim_token"])
        verticals = ("real_estate", "investors") if row["vertical"] == "both" else (row["vertical"],)
        e = Evidence(
            post_id=str(row["id"]),
            source_id=str(row["source_id"]),
            canonical_url=row["canonical_url"],
            text=row["body_text"],
            title=row["title"],
            published_at=row["published_at"],
            comments=[str(c) for c in json.loads(row["comments"] or "[]")],
        )
        context = await _context(store, e.post_id)
        with costs.scope(context.campaign_id if context else None):
            outcome = await _analyse_post(store, pipeline, e, verticals, token, model, context)
        if outcome == "halt":
            for later in rows[index + 1:]:  # the key is dead for every post: hand the rest back untouched
                await store.release(str(later["id"]), str(later["analysis_claim_token"]))
            break
        if isinstance(outcome, list):
            for vertical, fid, text in outcome:
                formatted[vertical].append((fid, text))
    return formatted, len(rows)


BLOCKING_HTTP = frozenset({401, 402})  # the key or the account: no other post can succeed either


def _host(url: str | None) -> str:
    try:
        return (urlsplit(url or "").hostname or "").removeprefix("www.")
    except ValueError:
        return ""


async def _context(store, post_id: str) -> TaskContext | None:
    if not hasattr(store, "task_context"):
        return None
    try:
        return await store.task_context(post_id)
    except Exception as exc:  # noqa: BLE001 - the context only saves money; a failed lookup sends the post to the model
        log.warning("analysis.task_context_failed %s %s", post_id, type(exc).__name__)
        return None


async def _analyse_post(store, pipeline: AnalysisPipeline, e: Evidence, verticals: tuple[str, ...], token: str,
                        model: str, context: TaskContext | None) -> list[tuple[str, str, str]] | str | None:
    """The findings of one post (``[]``: none), ``"halt"`` (a dead key: stop the batch) or None (handed back)."""
    host = _host(e.canonical_url)
    if context is not None and await costs.over_budget(context.campaign_id):
        log.info("analysis.budget_cap %s", e.post_id)
        await costs.skip("llm", "budget_cap", item=host)
        await store.finalize(e.post_id, token, accepted=False)
        return []
    reason = prefilter(e, context)
    if reason is not None:
        log.info("analysis.prefiltered %s %s", e.post_id, reason)
        await costs.skip("llm", f"prefilter_{reason}", item=host)
        await store.finalize(e.post_id, token, accepted=False)
        return []
    found: list[tuple[str, str, str]] = []
    try:
        hint = await store.task_hint(e.post_id) if hasattr(store, "task_hint") else None
    except Exception as exc:  # noqa: BLE001 - the hint only helps; a failed lookup must not stall the batch
        log.warning("analysis.task_hint_failed %s %s", e.post_id, type(exc).__name__)
        hint = None
    try:
        for vertical in verticals:
            try:
                result = await (pipeline.process(e, vertical, task_hint=hint) if hint else pipeline.process(e, vertical))
            except ValueError as exc:
                # The model answered outside the schema or refused the request: not a finding, and not retried --
                # but booked, so the campaign's report shows it instead of losing the page without a word.
                log.warning("analysis.invalid_model_response", extra={"post_id": e.post_id, "vertical": vertical})
                await costs.error("llm", str(exc)[:80] or "invalid_model_response", item=host)
                continue
            if not result.accepted:
                log.info("analysis.not_a_finding %s %s %s", e.post_id, vertical, result.reason)
            fid = await store.save(e, vertical, result, model, token)
            if fid:
                found.append((vertical, fid, result.formatted or ""))
    except httpx.HTTPError as exc:
        # OpenRouter unreachable, rate-limited or refusing the key: hand the post back; it is retried next cycle.
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        code = f"http_{status}" if status else f"network:{type(exc).__name__}"
        log.warning("analysis.model_unavailable %s", code, extra={"post_id": e.post_id})
        await costs.error("llm", code, item=host)
        await store.release(e.post_id, token)
        return "halt" if status in BLOCKING_HTTP else None
    if await store.finalize(e.post_id, token, accepted=bool(found)):
        return found
    return []


async def run_once(store=None, pipeline: AnalysisPipeline | None = None, send: Sender | None = None, settings: AnalysisSettings | None = None):
    s = settings or AnalysisSettings()
    own_store = store is None
    if own_store:
        store = PostgresAnalysisStore(s.database_url, exclude_platforms=s.excluded_platforms)
        await store.connect()
    try:
        p = pipeline or AnalysisPipeline(
            OpenRouterAnalyzer(s.openrouter_api_key, s.openrouter_model, timeout_seconds=s.timeout_seconds)
        )
        formatted, processed = await analyse_batch(store, p, batch_size=s.batch_size, claim_seconds=s.claim_seconds, model=s.openrouter_model)
        saved = [fid for entries in formatted.values() for fid, _ in entries]
        if s.telegram_token and s.telegram_chat_id:
            # A campaign's findings are streamed to its own chat by the campaign runner.
            streamed = await store.campaign_finding_ids(saved) if saved else set()
            for vertical, entries in formatted.items():
                for chunk in one_per_message([e for e in entries if e[0] not in streamed]):
                    await store.save_digest(s.telegram_chat_id, vertical, [fid for fid, _ in chunk], digest(vertical, [t for _, t in chunk]))
            token = s.telegram_token
            await deliver(store, s.telegram_chat_id, send or (lambda chat, body: send_digest(token, chat, body)))
        return {"findings": saved, "processed": processed}
    finally:
        if own_store:
            await store.close()


async def serve() -> None:
    """Long-running mode: analyse new evidence and deliver digests every poll interval."""
    s = AnalysisSettings()
    if not s.openrouter_api_key:
        log.warning("analysis.disabled", extra={"hint": "set OPENROUTER_API_KEY"})
        while True:
            await asyncio.sleep(3600)
    store = PostgresAnalysisStore(s.database_url, exclude_platforms=s.excluded_platforms)
    await store.connect()
    costs.install(costs.PostgresLedger(store._pool()), s.budget_usd)
    pipeline = AnalysisPipeline(OpenRouterAnalyzer(s.openrouter_api_key, s.openrouter_model, timeout_seconds=s.timeout_seconds))
    log.info("analysis.started", extra={"poll_seconds": s.poll_seconds, "digests": bool(s.telegram_chat_id)})
    try:
        while True:
            try:
                result = await run_once(store, pipeline, settings=s)
                if result["findings"]:
                    log.info("analysis.findings", extra={"count": len(result["findings"])})
                if result["processed"] >= s.batch_size:
                    continue  # a full batch: more may be waiting
            except Exception:
                log.exception("analysis.cycle_failed")
            await asyncio.sleep(s.poll_seconds)
    finally:
        await store.close()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if "--serve" in sys.argv:
        asyncio.run(serve())
    else:
        print(asyncio.run(run_once()))
