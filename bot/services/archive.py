"""The local, structured record of everything a search found.

Supabase keeps the handful of results that were sent to a user. This keeps
the whole run, on the machine the bot runs on:

    data/research/
      2026-09-21/
        20260921T143355-482913-<search-id>.json   one file per request
      listings.jsonl                              one line per listing, ever

The per-search file is the full picture -- the parsed request, every query
issued, every hit from every source (with the page text that was read), what
the ranker scored and what was filtered out before sending. The JSONL index
is the flat, greppable view across runs: one line per listing with its price,
location, contacts and source, so a month of searching can be read, exported
or re-analysed without repeating a single query.

Nothing here is allowed to break a search. Every public method swallows its
own I/O errors and logs them: a full disk is a reason to lose the archive,
never a reason to lose the answer the user is waiting for.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

from bot.config import ArchiveSettings
from bot.logging_conf import get_logger
from bot.models.enums import Mode
from bot.models.query import ParsedQuery, SearchQuery
from bot.models.result import PageContent, SearchHit, StoredResult, StructuredResult

log = get_logger(__name__)

LISTINGS_FILE = "listings.jsonl"
"""Append-only index across every run, one JSON object per line."""


class ResearchArchive:
    """Writes one JSON file per search, plus a rolling listings index."""

    def __init__(self, settings: ArchiveSettings) -> None:
        self.settings = settings
        self.root = Path(settings.dir).expanduser()

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def prepare(self) -> None:
        """Create the directory and drop anything past ``ARCHIVE_KEEP_DAYS``.

        Called once at start-up so a misconfigured path fails loudly there
        rather than silently swallowing every run's archive afterwards.
        """
        if not self.enabled:
            log.info("archive.disabled")
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("archive.mkdir_failed", dir=str(self.root), error=str(exc))
            return
        log.info("archive.ready", dir=str(self.root))
        self._prune()

    def save_run(
        self,
        *,
        search_id: UUID | None,
        user_id: int,
        mode: Mode,
        raw_query: str,
        transcript: str | None,
        parsed: ParsedQuery,
        queries: list[SearchQuery],
        counts: dict[str, Any],
        candidates: list[tuple[SearchHit, PageContent | None]],
        ranked: list[StructuredResult],
        sent: list[StoredResult],
        degraded: bool,
    ) -> Path | None:
        """Write one run to disk. Returns the file written, or ``None``.

        *candidates* is every hit that survived de-duplication, paired with
        whatever page text was read for it -- including the ones that never
        made it into the answer, which is the part Supabase never sees.
        """
        if not self.enabled:
            return None

        now = dt.datetime.now(dt.UTC)
        sent_urls = {row.url for row in sent}
        ranked_by_url = {result.url: result for result in ranked}

        document = {
            "schema": 1,
            "search_id": str(search_id) if search_id else None,
            "saved_at": now.isoformat(),
            "user_id": user_id,
            "mode": mode.value,
            "request": {
                "text": raw_query,
                "transcript": transcript,
                "parsed": parsed.model_dump(mode="json", exclude_none=True),
            },
            "queries": [query.model_dump(mode="json") for query in queries],
            "counts": counts,
            "degraded": degraded,
            "hits": [
                self._hit_entry(hit, page, ranked_by_url.get(hit.url), hit.url in sent_urls)
                for hit, page in candidates
            ],
            "sent": [self._listing(row, now, search_id) for row in sent],
        }

        path = self._run_path(now, search_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write and rename: a crash mid-write leaves the previous file
            # intact rather than a truncated one that will not parse.
            temporary = path.with_suffix(".json.part")
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(path)
        except OSError as exc:
            log.warning("archive.write_failed", path=str(path), error=str(exc))
            return None

        self._append_listings(document["sent"])
        log.info(
            "archive.saved",
            path=str(path),
            hits=len(document["hits"]),
            listings=len(document["sent"]),
        )
        return path

    # -- internals ---------------------------------------------------------

    def _run_path(self, now: dt.datetime, search_id: UUID | None) -> Path:
        """One directory per day, one file per run, sortable by name.

        The timestamp carries microseconds because two searches from two users
        can land in the same second, and the suffix is the search id when there
        is one so a file can be matched back to its Supabase row.
        """
        stamp = now.strftime("%Y%m%dT%H%M%S-%f")
        suffix = f"-{search_id}" if search_id else ""
        return self.root / now.strftime("%Y-%m-%d") / f"{stamp}{suffix}.json"

    def _hit_entry(
        self,
        hit: SearchHit,
        page: PageContent | None,
        ranked: StructuredResult | None,
        was_sent: bool,
    ) -> dict[str, Any]:
        """One candidate, with everything known about it after the run."""
        entry: dict[str, Any] = {
            "url": hit.url,
            "url_hash": hit.url_hash,
            "source": hit.source.value,
            "title": hit.title,
            "snippet": hit.snippet,
            "engines": hit.engines,
            "search_score": round(hit.score, 4),
            "query": hit.query,
            "author": hit.author,
            "published_at": hit.published_at.isoformat() if hit.published_at else None,
            "read": page is not None and page.ok,
            "sent": was_sent,
        }
        if page is not None and not page.ok:
            entry["read_error"] = page.error or f"http {page.status}"
        if page is not None and page.ok and self.settings.max_content_chars:
            entry["content"] = page.text[: self.settings.max_content_chars]
        if ranked is not None:
            # What the ranker made of it, including the ones it scored too low
            # to send -- that is the record of *why* something was dropped.
            entry["ranked"] = ranked.model_dump(mode="json", exclude_none=True)
        return entry

    def _listing(
        self, row: StoredResult, now: dt.datetime, search_id: UUID | None
    ) -> dict[str, Any]:
        """One sent listing, flattened for the cross-run index."""
        facts = row.raw or {}
        return {
            "saved_at": now.isoformat(),
            "search_id": str(search_id) if search_id else None,
            "user_id": row.user_id,
            "mode": row.mode.value,
            "source": row.source.value,
            "url": row.url,
            "url_hash": row.url_hash,
            "title": row.title,
            "summary": row.summary,
            "score": row.score,
            "location": facts.get("location"),
            "price": facts.get("price"),
            "price_value": facts.get("price_value"),
            "price_currency": facts.get("price_currency"),
            "area": facts.get("area"),
            "contacts": facts.get("contacts") or [],
            "why_relevant": facts.get("why_relevant"),
            "budget_fit": row.budget_fit.value,
            "budget_delta": row.budget_delta,
        }

    def _append_listings(self, listings: list[dict[str, Any]]) -> None:
        """Append to the flat index. One line per listing, newest last."""
        if not listings:
            return
        path = self.root / LISTINGS_FILE
        try:
            with path.open("a", encoding="utf-8") as handle:
                for listing in listings:
                    handle.write(json.dumps(listing, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("archive.index_failed", path=str(path), error=str(exc))

    def _prune(self) -> None:
        """Delete day directories older than ``keep_days``.

        Only the per-run files are pruned. The listings index is the long
        record and is never truncated here -- it is one line per listing, so
        it stays small next to the page text the run files carry.
        """
        if not self.settings.keep_days:
            return
        cutoff = dt.date.today() - dt.timedelta(days=self.settings.keep_days)
        removed = 0
        try:
            candidates = [entry for entry in self.root.iterdir() if entry.is_dir()]
        except OSError as exc:
            log.warning("archive.prune_failed", dir=str(self.root), error=str(exc))
            return

        for entry in candidates:
            try:
                day = dt.date.fromisoformat(entry.name)
            except ValueError:
                continue  # not one of ours; leave it alone
            if day >= cutoff:
                continue
            try:
                shutil.rmtree(entry)
                removed += 1
            except OSError as exc:
                log.warning("archive.prune_failed", path=str(entry), error=str(exc))
        if removed:
            log.info("archive.pruned", directories=removed, keep_days=self.settings.keep_days)


def load_listings(path: str | Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read the listings index back, newest first.

    Provided so the archive is usable and not just written: scripts and
    future export commands read it through here rather than each re-deriving
    the file layout.
    """
    file = Path(path)
    if file.is_dir():
        file = file / LISTINGS_FILE
    if not file.exists():
        return []

    rows: list[dict[str, Any]] = []
    with file.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A half-written last line (killed mid-append) must not make
                # the whole history unreadable.
                log.warning("archive.index_line_skipped", path=str(file))
    rows.reverse()
    return rows[:limit] if limit else rows


__all__ = ["LISTINGS_FILE", "ResearchArchive", "load_listings"]
