"""The research pipeline.

One user message goes in, a list of stored, ranked results comes out::

    text or voice
      -> transcribe (voice only)
      -> LLM: extract ParsedQuery
      -> QueryBuilder: search strings
      -> SearXNG: hits, merged and de-duplicated
      -> Supabase: drop hits this user has already seen
      -> fetch and extract the most promising pages
      -> LLM: rank, filter and summarise
      -> Supabase: store, enforcing UNIQUE (user_id, url_hash)

Every stage after extraction degrades rather than fails: if page fetching is
off or unproductive the LLM ranks on snippets; if ranking itself fails the raw
hits are returned with their snippets as summaries; if Supabase is unavailable
the results are still sent, just not remembered. Only a failure to understand
the request at all aborts the run, because there is nothing to search for.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field

from bot.config import Settings
from bot.exceptions import LLMError
from bot.logging_conf import get_logger
from bot.models.enums import Mode
from bot.models.query import ParsedQuery, SearchQuery
from bot.models.result import PageContent, SearchHit, StoredResult, StructuredResult
from bot.prompts import (
    DETAILS_SYSTEM,
    EXTRACT_SYSTEM,
    RANK_SYSTEM,
    build_details_prompt,
    build_extract_prompt,
    build_rank_prompt,
)
from bot.services.budget import BudgetMatch, split_by_fit
from bot.services.llm import ChatMessage
from bot.utils.text import plural_ru, truncate

if TYPE_CHECKING:
    from bot.services.db import SupabaseRepository
    from bot.services.llm import LLMManager
    from bot.services.parser import PageFetcher
    from bot.services.search import QueryBuilder, SearXNGClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]
"""Called with a short status line so the handler can keep the user informed."""


class RankedResults(BaseModel):
    """Wrapper the LLM fills in; a bare list is not a valid JSON-schema root
    for several providers' JSON modes."""

    results: list[StructuredResult] = Field(default_factory=list)


class PipelineOutcome(BaseModel):
    """Everything the handler needs to report on one request."""

    search_id: UUID | None = None
    parsed: ParsedQuery
    queries: list[SearchQuery] = Field(default_factory=list)
    hits_found: int = 0
    results: list[StoredResult] = Field(default_factory=list)
    """Matches first, then near misses. Ordering is what the handler sends."""
    exact_count: int = 0
    """How many of `results` are in budget. The rest are alternatives."""
    duplicates_skipped: int = 0
    degraded: bool = False
    """True when ranking fell back to raw search hits."""

    @property
    def is_empty(self) -> bool:
        return not self.results

    @property
    def alternatives(self) -> list[StoredResult]:
        """Results offered as near misses because their price was out of range."""
        return self.results[self.exact_count :]

    @property
    def only_alternatives(self) -> bool:
        """Nothing was in budget, but something close was found.

        This is the case the user is told about explicitly -- an empty answer
        and "nothing in your range, but here is one 45 000 more" are very
        different outcomes.
        """
        return self.exact_count == 0 and bool(self.results)


class ResearchPipeline:
    """Orchestrates the services. Holds no per-request state."""

    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMManager,
        search: SearXNGClient,
        query_builder: QueryBuilder,
        fetcher: PageFetcher,
        repo: SupabaseRepository,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.search = search
        self.query_builder = query_builder
        self.fetcher = fetcher
        self.repo = repo

    async def run(
        self,
        *,
        user_id: int,
        mode: Mode,
        text: str,
        transcript: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> PipelineOutcome:
        """Research *text* for *user_id* and return what should be sent."""
        started = time.monotonic()

        async def report(message: str) -> None:
            if progress is not None:
                await progress(message)

        parsed = await self.extract_query(text, mode)
        log.info("pipeline.parsed", user_id=user_id, summary=parsed.summary())

        queries = self.query_builder.build(parsed)
        search_id = await self.repo.create_search(
            user_id=user_id,
            mode=mode,
            raw_query=text,
            parsed=parsed,
            transcript=transcript,
            queries=queries,
        )

        described = parsed.human_summary() or text
        await report(
            f"🔎 Ищу: {described}\n"
            f"Поисковых запросов: {len(queries)}"
        )
        hits = await self.search.search_many(queries)
        log.info("pipeline.hits", user_id=user_id, hits=len(hits), queries=len(queries))

        if not hits:
            return PipelineOutcome(search_id=search_id, parsed=parsed, queries=queries)

        fresh_hits, duplicates = await self._drop_seen(user_id, hits)
        if not fresh_hits:
            return PipelineOutcome(
                search_id=search_id,
                parsed=parsed,
                queries=queries,
                hits_found=len(hits),
                duplicates_skipped=duplicates,
            )

        noun = plural_ru(len(fresh_hits), "ссылка", "ссылки", "ссылок")
        await report(f"📄 Найдено {len(fresh_hits)} {noun}, изучаю содержимое…")
        candidates = await self._collect_content(fresh_hits)

        degraded = False
        try:
            structured = await self.rank(parsed, candidates)
        except LLMError as exc:
            # The search worked; only the ranking failed. Sending unranked hits
            # is a much better outcome than sending nothing.
            log.warning("pipeline.rank.degraded", user_id=user_id, error=str(exc))
            structured = _fallback_results(fresh_hits, limit=self.settings.pipeline.max_results_to_user)
            degraded = True

        # In the degraded path the scores are placeholders, not judgements, so
        # applying the relevance threshold to them would discard everything for
        # anyone who raised PIPELINE_MIN_SCORE.
        relevant = (
            structured
            if degraded
            else [r for r in structured if r.score >= self.settings.pipeline.min_score]
        )
        keep, verdicts, exact_count = self._select(relevant, parsed)
        log.info(
            "pipeline.ranked",
            user_id=user_id,
            ranked=len(structured),
            matches=exact_count,
            alternatives=len(keep) - exact_count,
            degraded=degraded,
        )

        stored = await self._persist(
            keep,
            verdicts=verdicts,
            candidates=candidates,
            user_id=user_id,
            mode=mode,
            search_id=search_id,
        )
        # Persistence drops rows this user already has, which can shift the
        # boundary; recount rather than trusting the pre-save split.
        exact_count = sum(1 for row in stored if not row.is_alternative)

        if search_id is not None:
            await self.repo.finish_search(
                search_id, hits_found=len(hits), results_sent=len(stored)
            )

        log.info(
            "pipeline.done",
            user_id=user_id,
            results=len(stored),
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        return PipelineOutcome(
            search_id=search_id,
            parsed=parsed,
            queries=queries,
            hits_found=len(hits),
            results=stored,
            exact_count=exact_count,
            duplicates_skipped=duplicates,
            degraded=degraded,
        )

    # -- stages ------------------------------------------------------------

    async def extract_query(self, text: str, mode: Mode) -> ParsedQuery:
        """Turn free-form text into a :class:`ParsedQuery`.

        The only stage allowed to abort the run: without an understood request
        there is nothing to search for.
        """
        parsed = await self.llm.chat_structured(
            [ChatMessage.system(EXTRACT_SYSTEM), ChatMessage.user(build_extract_prompt(text, mode))],
            ParsedQuery,
            model=self.settings.llm.extract_model,
            purpose="extract",
        )
        # The user picked the mode with a button; the model does not get to
        # overrule that.
        return parsed.model_copy(update={"mode": mode})

    async def rank(
        self, parsed: ParsedQuery, candidates: list[tuple[SearchHit, PageContent | None]]
    ) -> list[StructuredResult]:
        """Score, filter and summarise the candidates."""
        if not candidates:
            return []

        prompt = build_rank_prompt(
            parsed,
            candidates,
            max_results=self.settings.pipeline.max_results_to_user,
            max_chars_per_page=self.settings.parser.max_chars,
        )
        ranked = await self.llm.chat_structured(
            [ChatMessage.system(RANK_SYSTEM), ChatMessage.user(prompt)],
            RankedResults,
            model=self.settings.llm.rank_model,
            purpose="rank",
        )

        # Models occasionally return a rewritten or hallucinated URL. Anything
        # that is not one of the URLs we supplied is dropped rather than sent.
        allowed = {hit.url for hit, _ in candidates}
        kept: list[StructuredResult] = []
        for result in ranked.results:
            if result.url in allowed:
                kept.append(result)
            else:
                log.warning("pipeline.rank.unknown_url", url=result.url)

        kept.sort(key=lambda r: r.score, reverse=True)
        return kept

    async def details(self, parsed: ParsedQuery, result: StoredResult) -> str:
        """Long-form briefing for the 'Подробнее' button.

        Re-fetches the page when the stored content is missing -- results saved
        before the parser ran, or truncated at storage time.
        """
        content = result.content or ""
        if not content:
            page = await self.fetcher.fetch(result.url)
            content = page.text if page.ok else ""
        if not content:
            return (
                "Не удалось получить дополнительную информацию: страница недоступна "
                "или закрыта от автоматического чтения. Откройте ссылку вручную."
            )

        response = await self.llm.chat(
            [
                ChatMessage.system(DETAILS_SYSTEM),
                ChatMessage.user(build_details_prompt(parsed, result.url, result.title, content)),
            ],
            model=self.settings.llm.rank_model,
            purpose="details",
        )
        return response.text.strip()

    # -- helpers -----------------------------------------------------------

    def _select(
        self, results: list[StructuredResult], query: ParsedQuery
    ) -> tuple[list[StructuredResult], dict[str, BudgetMatch], int]:
        """Choose what to send: matches first, then the closest near misses.

        Returns the chosen results, their budget verdicts keyed by URL, and how
        many of the chosen ones are actually in budget.

        Alternatives fill whatever room the matches leave, so a request with
        plenty of in-budget results is unaffected, while one with none still
        comes back with something useful instead of a shrug.
        """
        limit = self.settings.pipeline.max_results_to_user
        matches, alternatives = split_by_fit(results, query)
        verdicts = {result.url: match for result, match in (*matches, *alternatives)}

        chosen = [result for result, _ in matches][:limit]

        if self.settings.pipeline.include_alternatives:
            room = min(
                limit - len(chosen),
                self.settings.pipeline.max_alternatives,
            )
            if room > 0:
                chosen += [result for result, _ in alternatives[:room]]

        return chosen, verdicts, min(len(matches), limit)

    async def _drop_seen(self, user_id: int, hits: list[SearchHit]) -> tuple[list[SearchHit], int]:
        """Remove hits this user was already shown, if we can tell."""
        if not self.settings.pipeline.skip_seen_results:
            return hits, 0

        unseen = await self.repo.filter_unseen(user_id, [hit.url_hash for hit in hits])
        fresh = [hit for hit in hits if hit.url_hash in unseen]
        skipped = len(hits) - len(fresh)
        if skipped:
            log.info("pipeline.duplicates_skipped", user_id=user_id, count=skipped)
        return fresh, skipped

    async def _collect_content(
        self, hits: list[SearchHit]
    ) -> list[tuple[SearchHit, PageContent | None]]:
        """Fetch the top hits and pair every hit with its page, if any.

        Only ``PARSER_MAX_PAGES`` hits are fetched -- the rest still reach the
        ranker with their snippets, which is often enough to score them.
        """
        if not self.settings.parser.enabled:
            return [(hit, None) for hit in hits]

        to_fetch = hits[: self.settings.parser.max_pages]
        pages = await self.fetcher.fetch_many([hit.url for hit in to_fetch])
        return [(hit, pages.get(hit.url)) for hit in hits]

    async def _persist(
        self,
        results: list[StructuredResult],
        *,
        verdicts: dict[str, BudgetMatch],
        candidates: list[tuple[SearchHit, PageContent | None]],
        user_id: int,
        mode: Mode,
        search_id: UUID | None,
    ) -> list[StoredResult]:
        """Store the kept results, carrying their page text and budget verdict."""
        if not results:
            return []

        content_by_url = {
            hit.url: page.text for hit, page in candidates if page is not None and page.ok
        }
        rows = []
        for result in results:
            verdict = verdicts.get(result.url) or BudgetMatch()
            rows.append(
                StoredResult.from_structured(
                    result,
                    user_id=user_id,
                    mode=mode,
                    search_id=search_id,
                    content=content_by_url.get(result.url),
                    budget_fit=verdict.fit,
                    budget_delta=verdict.delta,
                    budget_currency=verdict.currency,
                )
            )

        stored = await self.repo.save_results(rows)
        # save_results returns only the rows it accepted, and in its own order;
        # restore the ranking order so matches still precede alternatives.
        position = {row.url: index for index, row in enumerate(rows)}
        stored.sort(key=lambda row: position.get(row.url, len(position)))
        return stored


def _fallback_results(hits: list[SearchHit], *, limit: int) -> list[StructuredResult]:
    """Raw hits dressed as results, for when the ranker is unavailable.

    The score is a placeholder: nothing has judged these. The caller skips the
    relevance threshold in this path, and the summary is the engine's own
    snippet -- honest, if unpolished.
    """
    return [
        StructuredResult(
            url=hit.url,
            title=truncate(hit.title or hit.url, 90),
            summary=hit.snippet or "Описание недоступно — откройте ссылку, чтобы посмотреть.",
            score=50,
            language=None,
        )
        for hit in hits[:limit]
    ]
