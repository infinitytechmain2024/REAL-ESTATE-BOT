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

import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field

from bot.config import Settings
from bot.exceptions import LLMError
from bot.logging_conf import get_logger
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery, QueryCriterion, SearchQuery
from bot.models.result import PageContent, SearchHit, StoredResult, StructuredResult
from bot.prompts import (
    DETAILS_SYSTEM,
    EXTRACT_SYSTEM,
    LOCATION_REPAIR_SYSTEM,
    RANK_SYSTEM,
    build_details_prompt,
    build_extract_prompt,
    build_location_repair_prompt,
    build_rank_prompt,
)
from bot.services.budget import BudgetMatch, split_by_fit
from bot.services.facts import extract_listing_facts
from bot.services.llm import ChatMessage
from bot.services.relevance import most_promising
from bot.utils.text import plural_ru, truncate

if TYPE_CHECKING:
    from bot.services.db import SupabaseRepository
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


class SourceGroup(BaseModel):
    """A discovered group, kept visible even if no matching posts can be read."""

    url: str
    title: str
    access: str | None = None


class SourceSearchResult(BaseModel):
    """Completed hits plus whether any part of this source could not be read.

    An aborted job may return earlier, fully extracted hits for display. A
    failed result's hits must not be persisted as a successfully completed job.
    """

    hits: list[SearchHit] = Field(default_factory=list)
    failed: bool = False
    notes: list[str] = Field(default_factory=list)
    groups: list[SourceGroup] = Field(default_factory=list)


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
    source_groups: list[SourceGroup] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)
    failed_sources: list[str] = Field(default_factory=list)
    """Unavailable sources, distinct from successful reads with no matches."""
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
        fetcher: Fetcher,
        repo: SupabaseRepository,
        sources: dict[str, Callable[[ParsedQuery], Awaitable[SourceSearchResult]]] | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.search = search
        self.query_builder = query_builder
        self.fetcher = fetcher
        self.repo = repo
        # Opt-in only: production Facebook wiring waits for the live Stage 1 probe.
        self.sources = sources or {}

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
        failed_sources: list[str] = []
        source_notes: list[str] = []
        source_groups: dict[str, SourceGroup] = {}
        unpersisted_urls: set[str] = set()
        for name, search_source in self.sources.items():
            result = await search_source(parsed)
            hits.extend(result.hits)
            source_groups.update({group.url: group for group in result.groups})
            source_notes.extend(note for note in result.notes if note not in source_notes)
            if result.failed:
                failed_sources.append(name)
                unpersisted_urls.update(hit.url for hit in result.hits)
        hits = _merge_hits(hits)
        log.info("pipeline.hits", user_id=user_id, hits=len(hits), queries=len(queries))

        if not hits:
            return PipelineOutcome(
                search_id=search_id, parsed=parsed, queries=queries, failed_sources=failed_sources,
                source_notes=source_notes,
                source_groups=list(source_groups.values()),
            )

        fresh_hits, duplicates = await self._drop_seen(user_id, hits)
        if not fresh_hits:
            return PipelineOutcome(
                search_id=search_id,
                parsed=parsed,
                queries=queries,
                hits_found=len(hits),
                duplicates_skipped=duplicates,
                failed_sources=failed_sources,
                source_notes=source_notes,
                source_groups=list(source_groups.values()),
            )

        noun = plural_ru(len(fresh_hits), "ссылка", "ссылки", "ссылок")
        await report(f"📄 Найдено {len(fresh_hits)} {noun}, изучаю содержимое…")
        # Reading a whole group can produce hundreds of posts, and the ranking
        # call is one prompt. Order them by what is countable in the text -- the
        # place, the wording, an area, a price -- and judge the best of them.
        # Nothing is dropped for being unreadable, only for being outranked.
        promising = most_promising(
            [(hit, None) for hit in fresh_hits],
            parsed,
            limit=self.settings.pipeline.max_candidates_to_rank,
        )
        if len(promising) < len(fresh_hits):
            log.info(
                "pipeline.candidates.trimmed",
                user_id=user_id,
                found=len(fresh_hits),
                ranked=len(promising),
            )
        candidates = await self._collect_content([hit for hit, _ in promising])

        degraded = False
        try:
            structured = await self.rank(parsed, candidates)
        except LLMError as exc:
            # The search worked; only the ranking failed. Sending unranked hits
            # is a much better outcome than sending nothing.
            log.warning("pipeline.rank.degraded", user_id=user_id, error=str(exc))
            structured = _fallback_results(
                fresh_hits, query=parsed, limit=self.settings.pipeline.max_results_to_user
            )
            degraded = True

        source_by_url = {hit.url: list(dict.fromkeys(hit.engines)) for hit in fresh_hits}
        structured = [
            result.model_copy(update={"sources": source_by_url.get(result.url, result.sources)})
            for result in structured
        ]

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
            unpersisted_urls=unpersisted_urls,
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
            failed_sources=failed_sources,
            source_notes=source_notes,
            source_groups=list(source_groups.values()),
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
            model=self.settings.llm.model_for("extract"),
            purpose="extract",
        )
        parsed = await self._repair_location(text, parsed)
        # The user picked the mode with a button; the model does not get to
        # overrule that. Recover a few high-value constraints from the raw
        # request when a local model omits them.
        parsed = _recover_critical_query_fields(text, parsed)
        return parsed.model_copy(update={"mode": mode})

    async def _repair_location(self, text: str, parsed: ParsedQuery) -> ParsedQuery:
        """Retry only the place when the main extraction did not normalise it."""
        location = parsed.location
        if any((location.city, location.region, location.country)):
            return parsed
        try:
            repaired = await self.llm.chat_structured(
                [
                    ChatMessage.system(LOCATION_REPAIR_SYSTEM),
                    ChatMessage.user(build_location_repair_prompt(text, location.raw)),
                ],
                Location,
                model=self.settings.llm.model_for("extract"),
                purpose="extract_location_repair",
            )
        except LLMError as exc:
            log.warning("pipeline.location_repair_failed", error=str(exc))
            return parsed
        if not any((repaired.city, repaired.region, repaired.country)):
            return parsed
        raw = repaired.raw or location.raw
        normalised_text = " ".join(text.casefold().split())
        normalised_raw = " ".join((raw or "").casefold().split())
        if not normalised_raw or normalised_raw not in normalised_text:
            log.warning(
                "pipeline.location_repair_rejected",
                raw=raw,
                city=repaired.city,
                country=repaired.country,
            )
            return parsed
        if repaired.raw != raw:
            repaired = repaired.model_copy(update={"raw": raw})
        return parsed.model_copy(update={"location": repaired})

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
            model=self.settings.llm.model_for("rank"),
            purpose="rank",
        )

        # Models occasionally return a rewritten or hallucinated URL. Anything
        # that is not one of the URLs we supplied is dropped rather than sent.
        allowed = {hit.url for hit, _ in candidates}
        sources_by_url = {hit.url: list(dict.fromkeys(hit.engines)) for hit, _ in candidates}
        kept: list[StructuredResult] = []
        for result in ranked.results:
            if result.url in allowed:
                kept.append(result.model_copy(update={"sources": sources_by_url.get(result.url, [])}))
            else:
                log.warning("pipeline.rank.unknown_url", url=result.url)

        kept.sort(key=lambda r: r.score, reverse=True)
        return kept

    async def details(self, parsed: ParsedQuery, result: StoredResult) -> str:
        """Long-form briefing for the 'Подробнее' button.

        Re-fetches the page when the stored content is missing -- results saved
        before the parser ran, or truncated at storage time.
        """
        if result.raw.get("source_kind") == "facebook_public":
            return (
                "Доступен только фрагмент публикации из веб-поиска. "
                "Полный текст не проверен — откройте ссылку на Facebook."
            )
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
            model=self.settings.llm.model_for("details", content_chars=len(content)),
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
        # Public Facebook discovery provides search snippets, not full posts.
        # Fetching their URLs would often rank a login wall as listing content.
        to_fetch_hits = [
            hit for hit in hits
            if hit.url not in pre_read and "facebook_public" not in hit.engines
        ]

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
        unpersisted_urls: set[str] | None = None,
    ) -> list[StoredResult]:
        """Store the kept results, carrying their page text and budget verdict."""
        if not results:
            return []

        content_by_url = {
            hit.url: page.text for hit, page in candidates if page is not None and page.ok
        }
        public_post_urls = {hit.url for hit, _ in candidates if "facebook_public" in hit.engines}
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

            if result.url in public_post_urls:
                rows[-1].raw["source_kind"] = "facebook_public"

        # Aborted source jobs may return completed hits for display, but must
        # never commit a partial job. Their buttons remain absent (no row id).
        unpersisted_urls = unpersisted_urls or set()
        transient = [row for row in rows if row.url in unpersisted_urls]
        persistable = [row for row in rows if row.url not in unpersisted_urls]
        stored = (await self.repo.save_results(persistable) if persistable else []) + transient
        # save_results returns only the rows it accepted, and in its own order;
        # restore the ranking order so matches still precede alternatives.
        position = {row.url: index for index, row in enumerate(rows)}
        stored.sort(key=lambda row: position.get(row.url, len(position)))
        return stored


def _recover_critical_query_fields(text: str, parsed: ParsedQuery) -> ParsedQuery:
    """Fill critical fields a small/local model occasionally leaves empty.

    This is intentionally conservative: it recognises only unambiguous
    phrases. Explicit values in the raw request win over the model because a
    missing or misread hard constraint changes which listings are acceptable.
    The LLM remains the source for the rest of the schema.
    """
    lowered = text.casefold()
    updates: dict[str, object] = {}

    area_match = re.search(
        r"(?:від|от|from|at\s+least)\s*(\d[\d\s]*(?:[.,]\d+)?)\s*"
        r"(?:м(?:\s*2|²|\s+квадратн\w*)?|m(?:\s*2|²)|square\s+met(?:er|re)s?)\b",
        lowered,
    )
    explicit_area: float | None = None
    if area_match:
        raw_area = area_match.group(1).replace(" ", "").replace(",", ".")
        with suppress(ValueError):
            explicit_area = float(raw_area)
            updates["area_min"] = explicit_area

    duration = r"(\d{1,3})\s*(?:хв(?:илин\w*)?|мин(?:ут\w*)?|minutes?)\b"
    driving = r"(?:на\s+(?:машин\w*|авто(?:мобил\w*)?)|by\s+car|driv\w*)"
    metro_match = re.search(
        rf"(?:метро|metro).{{0,50}}?{duration}.{{0,24}}?{driving}", lowered
    ) or re.search(rf"{duration}.{{0,24}}?{driving}.{{0,50}}?(?:метро|metro)", lowered)
    if metro_match:
        updates["metro_drive_minutes"] = int(metro_match.group(1))

    buildable_negative = re.search(
        r"(?:не\s+(?:для\s+)?застройк\w*|не\s+для\s+забудови|not\s+buildable|"
        r"not\s+for\s+(?:construction|development)|no\s+urbanizable)",
        lowered,
    )
    buildable_positive = re.search(
        r"для\s+забудови|для\s+застройки|для\s+строительств\w*|"
        r"buildable|for\s+(?:construction|development)|urbanizable|edificable",
        lowered,
    )
    if buildable_negative:
        updates["buildable_required"] = False
    elif buildable_positive:
        updates["buildable_required"] = True

    building_optional = re.search(
        r"з\s+будинками?\s+або\s+без|с\s+дом\w*\s+или\s+без|"
        r"with\s+or\s+without\s+(?:a\s+)?house|con\s+o\s+sin\s+casa",
        lowered,
    )
    if building_optional:
        updates["building_required"] = None

    land_request = re.search(
        r"земельн\w*\s+(?:участк\w*|ділян\w*)|земельн\w*\s+ділян\w*|land\s+plots?",
        lowered,
    )
    if parsed.object_type is None and land_request:
        updates["object_type"] = "land plot"

    location = parsed.location
    suburb_match = re.search(
        r"пригород\w*|передміст\w*|suburb\w*|outskirt\w*|suburbio\w*|perifer\w*",
        lowered,
    )

    if location.country and location.country.casefold() in {"spain", "españa"}:
        languages = ["es", "en", *parsed.languages]
        updates["languages"] = list(dict.fromkeys(code.casefold() for code in languages if code))

    criteria = list(parsed.criteria)
    duplicate_markers: list[Callable[[str], bool]] = []
    if explicit_area is not None:
        area_token = f"{explicit_area:g}"
        area_pattern = re.compile(rf"(?<![\d.]){re.escape(area_token)}(?![\d.])")
        duplicate_markers.append(lambda value: bool(area_pattern.search(value)))
    if suburb_match:
        duplicate_markers.append(
            lambda value: any(
                marker in value
                for marker in ("suburb", "outskirt", "пригород", "передміст", "suburbio", "perifer")
            )
        )
    if metro_match:
        minute_token = metro_match.group(1)
        duplicate_markers.append(
            lambda value: "metro" in value
            or "метро" in value
            or (
                minute_token in value
                and any(unit in value for unit in ("minute", "хв", "мин"))
            )
        )
    if buildable_positive and not buildable_negative:
        duplicate_markers.append(
            lambda value: any(
                marker in value
                for marker in (
                    "buildable",
                    "development",
                    "construction",
                    "urbanizable",
                    "edificable",
                    "забудов",
                    "застрой",
                )
            )
            or set(value.split()) in ({"true"}, {"yes"})
        )
    if building_optional:
        duplicate_markers.append(
            lambda value: any(
                marker in value for marker in ("house", "building", "дом", "будин", "casa")
            )
            or any(marker in value for marker in ("with or without", "або без", "или без"))
        )
    if duplicate_markers:
        criteria = [
            criterion
            for criterion in criteria
            if not any(
                is_duplicate(f"{criterion.name} {criterion.value}".casefold())
                for is_duplicate in duplicate_markers
            )
        ]

    def set_criterion(name: str, value: str, importance: str) -> None:
        nonlocal criteria
        criteria = [criterion for criterion in criteria if criterion.name != name]
        criteria.append(QueryCriterion(name=name, value=value, importance=importance))

    if explicit_area is not None:
        set_criterion("area_min", f"{explicit_area:g} m²", "required")
    if suburb_match:
        location_value = (
            f"{location.city} suburbs" if location.city else (location.raw or "")
        )
        if location_value:
            set_criterion("location", location_value, "required")
    if metro_match:
        set_criterion(
            "metro_drive_minutes", f"{int(metro_match.group(1))} minutes by car", "required"
        )
    if buildable_positive and not buildable_negative:
        set_criterion("buildable_required", "suitable for construction", "required")
    if building_optional:
        set_criterion("building", "with or without a house", "optional")
    if criteria != parsed.criteria:
        updates["criteria"] = criteria

    return parsed.model_copy(update=updates) if updates else parsed


def _merge_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """Merge source hits while retaining provenance and the richest content."""
    merged: dict[str, SearchHit] = {}
    for hit in hits:
        existing = merged.get(hit.url_hash)
        if existing is None:
            merged[hit.url_hash] = hit
            continue
        existing.engines = list(dict.fromkeys([*existing.engines, *hit.engines]))
        existing.score = max(existing.score, hit.score)
        if len(hit.snippet) > len(existing.snippet):
            existing.snippet = hit.snippet
        if not existing.title and hit.title:
            existing.title = hit.title
        if existing.content is None and hit.content:
            existing.content = hit.content
        if existing.author is None and hit.author:
            existing.author = hit.author
        if existing.published_at is None and hit.published_at is not None:
            existing.published_at = hit.published_at
    return list(merged.values())


def _fallback_results(
    hits: list[SearchHit], *, query: ParsedQuery, limit: int
) -> list[StructuredResult]:
    """Raw hits dressed as results, for when the ranker is unavailable.

    The score is a placeholder: nothing has judged these. The caller skips the
    relevance threshold in this path, and the summary is the engine's own
    snippet -- honest, if unpolished.
    """
    missing = []
    if query.area_min is not None:
        missing.append(f"площадь от {query.area_min:g} м² не проверена")
    if query.metro_drive_minutes is not None:
        missing.append("расстояние до метро не проверено")
    if query.buildable_required:
        missing.append("назначение под застройку не проверено")
    return [
        StructuredResult(
            url=hit.url,
            title=truncate(hit.title or hit.url, 90),
            summary=hit.snippet or "Описание недоступно — откройте ссылку, чтобы посмотреть.",
            score=50,
            language=None,
            seller=hit.author,
            missing_criteria=missing,
            **extract_listing_facts(hit.content or hit.snippet),
        )
        for hit in hits[:limit]
    ]
