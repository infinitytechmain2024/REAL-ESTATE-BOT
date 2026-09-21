"""The research pipeline.

One user message goes in, a list of stored, ranked results comes out::

    text or voice
      -> transcribe (voice only)
      -> LLM: extract ParsedQuery
      -> QueryBuilder: search strings
      -> SearXNG *and* Facebook groups, concurrently
      -> one merged, de-duplicated, ranked hit list
      -> Supabase: drop hits this user has already seen
      -> fetch and extract the most promising pages
      -> LLM: rank, filter and summarise, in batches
      -> local archive: the whole run, as structured JSON
      -> Supabase: store, enforcing UNIQUE (user_id, url_hash)

The two sources are one search, not two. Web hits and Facebook group posts
are merged into a single list before anything ranks them, so a post and a
listing compete on relevance and the user gets one answer instead of two
lists to reconcile by hand. The source survives as a label on each result.

Every stage after extraction degrades rather than fails: if page fetching is
off or unproductive the LLM ranks on snippets; if a ranking batch fails only
its own candidates are lost; if every batch fails the raw hits are returned
with their snippets as summaries; if Supabase or the archive is unavailable
the results are still sent. Only a failure to understand the request at all
aborts the run, because there is nothing to search for.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field

from bot.config import Settings
from bot.exceptions import LLMError
from bot.logging_conf import get_logger
from bot.models.enums import HitSource, Mode
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
    from bot.services.archive import ResearchArchive
    from bot.services.db import SupabaseRepository
    from bot.services.facebook import FacebookSource
    from bot.services.llm import LLMManager
    from bot.services.parser import Fetcher
    from bot.services.search import QueryBuilder, SearXNGClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]
"""Called with a short status line so the handler can keep the user informed."""


class RankedResults(BaseModel):
    """Wrapper the LLM fills in; a bare list is not a valid JSON-schema root
    for several providers' JSON modes."""

    results: list[StructuredResult] = Field(default_factory=list)


class SearchStats(BaseModel):
    """What the search actually did, in numbers the user can be told.

    Reporting one number for all of this is what produced the same suspicious
    "found 40 links" on every request: that 40 was the merge cap, not a
    finding. These are the real quantities -- how many entries the engines and
    groups returned, how many distinct links that came to, how many were new
    for this user, and how many pages were opened and read.
    """

    raw: int = 0
    """Entries returned by every source, before de-duplication."""
    unique: int = 0
    """Distinct links after merging web and Facebook hits."""
    web: int = 0
    """Of `unique`, how many came from the open web."""
    facebook: int = 0
    """Of `unique`, how many came from Facebook groups. Adds up with `web`."""
    fresh: int = 0
    """Links left after dropping the ones this user was already shown."""
    read: int = 0
    """Pages whose text was actually read (fetched, or read in the group)."""
    truncated: bool = False
    """Whether SEARXNG_MAX_HITS cut the merged list short."""
    failed_queries: int = 0
    """Search queries that errored out."""

    def per_source(self) -> dict[str, int]:
        """Non-zero source counts, for the 'where these came from' line."""
        return {name: count for name, count in (("web", self.web), ("facebook", self.facebook)) if count}


class PipelineOutcome(BaseModel):
    """Everything the handler needs to report on one request."""

    search_id: UUID | None = None
    parsed: ParsedQuery
    queries: list[SearchQuery] = Field(default_factory=list)
    hits_found: int = 0
    """Distinct links found across every source. Kept as its own field because
    it is what the closing summary quotes."""
    stats: SearchStats = Field(default_factory=SearchStats)
    results: list[StoredResult] = Field(default_factory=list)
    """Matches first, then near misses. Ordering is what the handler sends."""
    exact_count: int = 0
    """How many of `results` are in budget. The rest are alternatives."""
    duplicates_skipped: int = 0
    degraded: bool = False
    """True when ranking fell back to raw search hits."""
    archive_path: str | None = None
    """Where this run was written on disk, when the archive is on."""

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
        fetcher: Fetcher,
        repo: SupabaseRepository,
        facebook: FacebookSource | None = None,
        archive: ResearchArchive | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.search = search
        self.query_builder = query_builder
        self.fetcher = fetcher
        self.repo = repo
        self.facebook = facebook
        """Second hit source, merged into the same list. None when Facebook is off."""
        self.archive = archive
        """Local structured record of every run. None when the archive is off."""

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
        sources = "интернет" + (" и Facebook-группы" if self._facebook_active else "")
        await report(
            f"🔎 Ищу: {described}\n"
            f"Источники: {sources}\n"
            f"Поисковых запросов: {len(queries)}"
        )

        hits, stats = await self._gather_hits(parsed, queries)
        log.info(
            "pipeline.hits",
            user_id=user_id,
            queries=len(queries),
            **stats.model_dump(exclude={"fresh", "read"}),
        )

        if not hits:
            return PipelineOutcome(
                search_id=search_id, parsed=parsed, queries=queries, stats=stats
            )

        fresh_hits, duplicates = await self._drop_seen(user_id, hits)
        stats.fresh = len(fresh_hits)
        if not fresh_hits:
            return PipelineOutcome(
                search_id=search_id,
                parsed=parsed,
                queries=queries,
                hits_found=stats.unique,
                stats=stats,
                duplicates_skipped=duplicates,
            )

        # Report the split of what was found, not one lumped total: the counts
        # differ per request, they add up, and the user can see which source
        # carried the answer.
        noun = plural_ru(stats.unique, "ссылка", "ссылки", "ссылок")
        lines = [f"📄 Найдено {stats.unique} {noun} ({_source_line(stats)})"]
        if duplicates:
            lines.append(f"Из них уже показывал раньше: {duplicates}")
        lines.append(f"Изучаю содержимое: {len(fresh_hits)}…")
        await report("\n".join(lines))

        candidates = await self._collect_content(fresh_hits)
        stats.read = sum(1 for _, page in candidates if page is not None and page.ok)

        await report(
            f"🧩 Прочитано страниц: {stats.read} из {len(fresh_hits)}. "
            "Отбираю подходящие объекты…"
        )
        structured, degraded = await self._rank_all(parsed, candidates, user_id=user_id)
        if degraded:
            structured = _fallback_results(
                fresh_hits, limit=self.settings.pipeline.max_results_to_user
            )

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
                search_id, hits_found=stats.unique, results_sent=len(stored)
            )

        archive_path = await self._archive_run(
            search_id=search_id,
            user_id=user_id,
            mode=mode,
            raw_query=text,
            transcript=transcript,
            parsed=parsed,
            queries=queries,
            stats=stats,
            duplicates=duplicates,
            candidates=candidates,
            ranked=structured,
            sent=stored,
            degraded=degraded,
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
            hits_found=stats.unique,
            stats=stats,
            results=stored,
            exact_count=exact_count,
            duplicates_skipped=duplicates,
            degraded=degraded,
            archive_path=str(archive_path) if archive_path else None,
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

    @property
    def _facebook_active(self) -> bool:
        """Whether the Facebook source will actually contribute anything."""
        return self.facebook is not None and self.facebook.settings.enabled and bool(
            self.facebook.settings.group_urls
        )

    async def _gather_hits(
        self, parsed: ParsedQuery, queries: list[SearchQuery]
    ) -> tuple[list[SearchHit], SearchStats]:
        """Search every source at once and merge the results into one list.

        The web search and the Facebook groups run concurrently and their hits
        go through the same merge, so the answer is one ranked list rather than
        a web section followed by a Facebook section. A URL found both ways
        keeps the group's read text and the stronger source label.

        A failing Facebook source costs only its own hits: the browser session
        may be logged out, mid-checkpoint, or reading a group whose layout
        changed, none of which should cost the user the web half of the answer.
        """

        async def facebook_hits() -> list[SearchHit]:
            if not self._facebook_active:
                return []
            assert self.facebook is not None
            try:
                return await self.facebook.search(parsed)
            except Exception as exc:  # noqa: BLE001 - one source must not sink the other
                log.warning("pipeline.facebook.failed", error=str(exc), exc_info=True)
                return []

        batch, fb_hits = await asyncio.gather(
            self.search.search_many(queries), facebook_hits()
        )

        # Re-merge web and Facebook hits together rather than concatenating:
        # de-duplication has to span the sources, or a listing cross-posted to
        # a group arrives twice.
        merged = self.search.merge_batch(
            [*batch.hits, *fb_hits],
            weights={query.query: query.weight for query in queries},
            limit=self.settings.searxng.max_hits,
        )
        hits = merged.hits

        stats = SearchStats(
            raw=batch.raw + len(fb_hits),
            unique=len(hits),
            web=sum(1 for hit in hits if hit.source is HitSource.WEB),
            facebook=sum(1 for hit in hits if hit.source is HitSource.FACEBOOK),
            truncated=batch.truncated or merged.truncated,
            failed_queries=batch.failed_queries,
        )
        return hits, stats

    async def _rank_all(
        self,
        parsed: ParsedQuery,
        candidates: list[tuple[SearchHit, PageContent | None]],
        *,
        user_id: int,
    ) -> tuple[list[StructuredResult], bool]:
        """Rank every candidate, in batches, and return them best first.

        The hit pool is deliberately much larger than one prompt should carry,
        so it is split into ``PIPELINE_RANK_BATCH_SIZE`` chunks ranked
        concurrently. That is also the failure boundary: a batch the model
        chokes on costs its own candidates, and the rest of the answer still
        arrives. Only losing every batch is degradation worth telling the user
        about, and that is what the second return value reports.
        """
        if not candidates:
            return [], False

        size = max(1, self.settings.pipeline.rank_batch_size)
        batches = [candidates[i : i + size] for i in range(0, len(candidates), size)]
        semaphore = asyncio.Semaphore(self.settings.pipeline.rank_concurrency)
        failures = 0

        async def rank_one(batch: list[tuple[SearchHit, PageContent | None]]) -> list[StructuredResult]:
            nonlocal failures
            async with semaphore:
                try:
                    return await self.rank(parsed, batch)
                except LLMError as exc:
                    failures += 1
                    log.warning(
                        "pipeline.rank.batch_failed",
                        user_id=user_id,
                        candidates=len(batch),
                        error=str(exc),
                    )
                    return []

        ranked_batches = await asyncio.gather(*(rank_one(batch) for batch in batches))
        ranked = [result for batch in ranked_batches for result in batch]
        ranked.sort(key=lambda result: result.score, reverse=True)

        log.info(
            "pipeline.rank.batches",
            user_id=user_id,
            batches=len(batches),
            failed=failures,
            ranked=len(ranked),
        )
        # Every batch failed: there is nothing ranked to show, so the caller
        # falls back to raw hits and says so.
        return ranked, failures == len(batches)

    async def _archive_run(
        self,
        *,
        search_id: UUID | None,
        user_id: int,
        mode: Mode,
        raw_query: str,
        transcript: str | None,
        parsed: ParsedQuery,
        queries: list[SearchQuery],
        stats: SearchStats,
        duplicates: int,
        candidates: list[tuple[SearchHit, PageContent | None]],
        ranked: list[StructuredResult],
        sent: list[StoredResult],
        degraded: bool,
    ) -> Path | None:
        """Write the whole run to the local archive.

        Everything found is kept, not just what was sent: the candidates that
        scored too low, the pages that would not open, the queries behind them.
        A failure here is logged and swallowed -- the user's answer outranks
        the record of it.

        The write runs in a thread: a run file carries the page text of
        everything that was read, so it is large enough that writing it on the
        event loop would stall every other request in flight.
        """
        if self.archive is None or not self.archive.enabled:
            return None
        try:
            return await asyncio.to_thread(
                self.archive.save_run,
                search_id=search_id,
                user_id=user_id,
                mode=mode,
                raw_query=raw_query,
                transcript=transcript,
                parsed=parsed,
                queries=queries,
                counts={**stats.model_dump(), "duplicates_skipped": duplicates},
                candidates=candidates,
                ranked=ranked,
                sent=sent,
                degraded=degraded,
            )
        except Exception:  # noqa: BLE001 - the user's answer outranks the archive
            log.warning("pipeline.archive.failed", exc_info=True)
            return None

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
        """Pair every hit with its page text, fetching what nothing already read.

        A hit that arrives with ``content`` pre-filled (e.g. a Facebook group
        post the facebook source already read) is never fetched -- the plain
        HTTP fetcher cannot reach an authenticated page anyway, and re-fetching
        would just throw away a real read. Only ``PARSER_MAX_PAGES`` of the
        *remaining* hits are fetched; the rest still reach the ranker with
        their snippets, which is often enough to score them.
        """
        pre_read = {
            hit.url: PageContent(url=hit.url, title=hit.title, text=hit.content)
            for hit in hits
            if hit.content
        }
        to_fetch_hits = [hit for hit in hits if hit.url not in pre_read]

        fetched: dict[str, PageContent] = {}
        if self.settings.parser.enabled and to_fetch_hits:
            to_fetch = to_fetch_hits[: self.settings.parser.max_pages]
            fetched = await self.fetcher.fetch_many([hit.url for hit in to_fetch])

        pages = {**fetched, **pre_read}
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
        # The ranker returns URLs, not hits, so the source label is carried
        # over from the candidate that produced each one -- it is the only
        # thing in the sent message that still says "this came from a group".
        source_by_url = {hit.url: hit.source for hit, _ in candidates}
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
                    source=source_by_url.get(result.url, HitSource.WEB),
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


def _source_line(stats: SearchStats) -> str:
    """'интернет: 112, Facebook: 7' -- where the links actually came from.

    Only sources that contributed are named, so a run with Facebook off does
    not advertise an empty section.
    """
    labels = {"web": "интернет", "facebook": "Facebook"}
    parts = [f"{labels[name]}: {count}" for name, count in stats.per_source().items()]
    return ", ".join(parts) or "источники недоступны"


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
