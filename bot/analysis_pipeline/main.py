from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import httpx

from .formatters import digest
from .models import Evidence
from .openrouter import OpenRouterAnalyzer
from .pipeline import AnalysisPipeline
from .settings import AnalysisSettings
from .store import PostgresAnalysisStore
from .telegram import send_digest

log = logging.getLogger(__name__)
Sender = Callable[[int, str], Awaitable[int]]
# Telegram's hard limit is 4096; a digest is split, never truncated.
MAX_MESSAGE_CHARS = 3900


def split_digest(entries: list[tuple[str, str]], limit: int = MAX_MESSAGE_CHARS) -> list[list[tuple[str, str]]]:
    """Group (finding id, text) entries into messages that each fit Telegram."""
    chunks: list[list[tuple[str, str]]] = []
    size = 0
    for fid, text in entries:
        text = text[:limit]
        if chunks and size + 2 + len(text) <= limit:
            chunks[-1].append((fid, text))
            size += 2 + len(text)
        else:
            chunks.append([(fid, text)])
            size = len(text)
    return chunks


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
    """Analyse one claimed batch; returns findings per vertical and how many posts were claimed."""
    formatted: dict[str, list[tuple[str, str]]] = {"real_estate": [], "investors": []}
    rows = await store.pending(batch_size, claim_seconds=claim_seconds)
    for row in rows:
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
        found: list[tuple[str, str, str]] = []
        try:
            for vertical in verticals:
                try:
                    result = await pipeline.process(e, vertical)
                except ValueError:
                    # The model answered outside the schema: not a finding, and not retried.
                    log.warning("analysis.invalid_model_response", extra={"post_id": e.post_id, "vertical": vertical})
                    continue
                if not result.accepted:
                    log.info("analysis.not_a_finding %s %s %s", e.post_id, vertical, result.reason)
                fid = await store.save(e, vertical, result, model, token)
                if fid:
                    found.append((vertical, fid, result.formatted or ""))
        except httpx.HTTPError:
            # OpenRouter unreachable: hand the post back; it is retried next cycle.
            log.warning("analysis.model_unavailable", extra={"post_id": e.post_id})
            await store.release(e.post_id, token)
            continue
        if await store.finalize(e.post_id, token, accepted=bool(found)):
            for vertical, fid, text in found:
                formatted[vertical].append((fid, text))
    return formatted, len(rows)


async def run_once(store=None, pipeline: AnalysisPipeline | None = None, send: Sender | None = None, settings: AnalysisSettings | None = None):
    s = settings or AnalysisSettings()
    own_store = store is None
    if own_store:
        store = PostgresAnalysisStore(s.database_url)
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
                for chunk in split_digest([e for e in entries if e[0] not in streamed]):
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
    store = PostgresAnalysisStore(s.database_url)
    await store.connect()
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
