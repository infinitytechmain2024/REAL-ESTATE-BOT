from __future__ import annotations

import asyncio

from .formatters import digest
from .models import Evidence
from .openrouter import OpenRouterAnalyzer
from .pipeline import AnalysisPipeline
from .settings import AnalysisSettings
from .store import PostgresAnalysisStore
from .telegram import send_digest


async def run_once():
    s = AnalysisSettings()
    store = PostgresAnalysisStore(s.database_url)
    await store.connect()
    try:
        p = AnalysisPipeline(
            OpenRouterAnalyzer(
                s.openrouter_api_key, s.openrouter_model, timeout_seconds=s.timeout_seconds
            )
        )
        saved = []
        formatted: dict[str, list[tuple[str, str]]] = {"real_estate": [], "investors": []}
        for row in await store.pending(s.batch_size):
            verticals = (
                ("real_estate", "investors") if row["vertical"] == "both" else (row["vertical"],)
            )
            e = Evidence(
                post_id=str(row["id"]),
                source_id=str(row["source_id"]),
                canonical_url=row["canonical_url"],
                text=row["body_text"],
                title=row["title"],
                published_at=row["published_at"],
            )
            for vertical in verticals:
                result = await p.process(e, vertical)
                fid = await store.save(e, vertical, result, s.openrouter_model)
                if fid:
                    saved.append(fid)
                    formatted[vertical].append((fid, result.formatted or ""))
        if s.telegram_token and s.telegram_chat_id:
            for vertical, entries in formatted.items():
                if not entries:
                    continue
                body = digest(vertical, [entry[1] for entry in entries])
                digest_id, duplicate = await store.save_digest(
                    s.telegram_chat_id, vertical, [entry[0] for entry in entries], body
                )
                if not duplicate:
                    message_id = await send_digest(s.telegram_token, s.telegram_chat_id, body)
                    await store.mark_digest_sent(digest_id, message_id)
        return {"findings": saved}
    finally:
        await store.close()


if __name__ == "__main__":
    print(asyncio.run(run_once()))
