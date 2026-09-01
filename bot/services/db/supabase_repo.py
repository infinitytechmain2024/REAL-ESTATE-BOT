"""Persistence on Supabase (PostgREST).

Everything the bot stores goes through this class. Two design decisions worth
knowing about:

* **Persistence is optional.** If ``SUPABASE_URL``/``SUPABASE_KEY`` are unset
  the repository runs in a disabled state: every write is a no-op and every
  read returns nothing. The bot still searches and answers -- it just cannot
  remember. That keeps local experimentation to a single env var.
* **Storage failures never break a search.** The pipeline has already spent
  LLM tokens and page fetches by the time it saves; losing the row is bad, but
  losing the user's answer over it is worse. Writes log and return ``None``.

De-duplication relies on the ``UNIQUE (user_id, url_hash)`` constraint from
``migrations/001_init.sql``; :meth:`save_results` uses ``upsert(...,
ignore_duplicates=True)`` so a repeat listing is silently skipped rather than
raising.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from postgrest import APIError
from supabase import AsyncClient, AsyncClientOptions, acreate_client

from bot.config import SupabaseSettings
from bot.logging_conf import get_logger
from bot.models.enums import Feedback, Mode, ResultStatus
from bot.models.query import ParsedQuery, SearchQuery
from bot.models.result import StoredResult

log = get_logger(__name__)


class SupabaseRepository:
    """Async data access for users, searches, results and feedback."""

    def __init__(self, settings: SupabaseSettings) -> None:
        self.settings = settings
        self._client: AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.configured

    async def connect(self) -> None:
        """Build the client. Safe to call when Supabase is not configured."""
        if not self.enabled:
            log.warning(
                "supabase.disabled",
                detail="SUPABASE_URL/SUPABASE_KEY are unset; results will not be persisted "
                "and cross-session de-duplication is off",
            )
            return
        assert self.settings.url and self.settings.key  # narrowed by `enabled`
        self._client = await acreate_client(
            self.settings.url,
            self.settings.key.get_secret_value(),
            options=AsyncClientOptions(
                schema=self.settings.schema_name,
                postgrest_client_timeout=int(self.settings.timeout_seconds),
                auto_refresh_token=False,
                persist_session=False,
            ),
        )
        log.info("supabase.connected", url=self.settings.url, schema=self.settings.schema_name)

    async def aclose(self) -> None:
        self._client = None

    # -- users -------------------------------------------------------------

    async def upsert_user(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        language_code: str | None = None,
    ) -> None:
        """Insert or refresh the user row. Called on every incoming update."""
        if self._client is None:
            return
        payload = {
            "telegram_id": telegram_id,
            "username": username,
            "first_name": first_name,
            "language_code": language_code,
        }
        await self._execute(
            "upsert_user",
            lambda: self._table("users").upsert(payload, on_conflict="telegram_id"),
        )

    async def set_current_mode(self, telegram_id: int, mode: Mode) -> None:
        """Remember the user's mode so it survives a restart."""
        if self._client is None:
            return
        await self._execute(
            "set_current_mode",
            lambda: self._table("users")
            .update({"current_mode": mode.value})
            .eq("telegram_id", telegram_id),
        )

    async def get_current_mode(self, telegram_id: int) -> Mode | None:
        """The user's last chosen mode, if we know it."""
        if self._client is None:
            return None
        rows = await self._execute(
            "get_current_mode",
            lambda: self._table("users")
            .select("current_mode")
            .eq("telegram_id", telegram_id)
            .limit(1),
        )
        if not rows:
            return None
        value = rows[0].get("current_mode")
        try:
            return Mode(value) if value else None
        except ValueError:
            return None

    # -- searches ----------------------------------------------------------

    async def create_search(
        self,
        *,
        user_id: int,
        mode: Mode,
        raw_query: str,
        parsed: ParsedQuery | None = None,
        transcript: str | None = None,
        queries: list[SearchQuery] | None = None,
    ) -> UUID | None:
        """Record the request. Returns the row id, or ``None`` if unavailable."""
        if self._client is None:
            return None
        payload: dict[str, Any] = {
            "user_id": user_id,
            "mode": mode.value,
            "raw_query": raw_query,
            "transcript": transcript,
            "parsed": parsed.model_dump(mode="json") if parsed else {},
            "queries": [q.model_dump(mode="json") for q in (queries or [])],
        }
        rows = await self._execute(
            "create_search", lambda: self._table("searches").insert(payload)
        )
        if not rows:
            return None
        try:
            return UUID(str(rows[0]["id"]))
        except (KeyError, ValueError):
            return None

    async def finish_search(self, search_id: UUID, *, hits_found: int, results_sent: int) -> None:
        """Write the outcome counters back onto the search row."""
        if self._client is None or search_id is None:
            return
        await self._execute(
            "finish_search",
            lambda: self._table("searches")
            .update({"hits_found": hits_found, "results_sent": results_sent})
            .eq("id", str(search_id)),
        )

    # -- results -----------------------------------------------------------

    async def filter_unseen(self, user_id: int, url_hashes: list[str]) -> set[str]:
        """Return the subset of *url_hashes* this user has not been shown.

        Called before the expensive part of the pipeline, so a user asking a
        similar question twice does not pay for pages they already rejected.
        Fails open: if the lookup errors, every hash is treated as unseen,
        because showing a result twice is a far smaller cost than dropping it.
        """
        if self._client is None or not url_hashes:
            return set(url_hashes)

        seen: set[str] = set()
        # PostgREST puts the filter in the query string, so a very long `in`
        # list can exceed the URL limit; chunk it.
        for chunk in _chunked(url_hashes, 100):
            rows = await self._execute(
                "filter_unseen",
                lambda chunk=chunk: self._table("results")
                .select("url_hash")
                .eq("user_id", user_id)
                .in_("url_hash", chunk),
            )
            seen.update(str(row["url_hash"]) for row in rows or [] if row.get("url_hash"))

        return {h for h in url_hashes if h not in seen}

    async def save_results(self, results: list[StoredResult]) -> list[StoredResult]:
        """Insert *results*, skipping any that violate the uniqueness constraint.

        Returns the rows as stored, with their database ids filled in -- the
        callback buttons need those. Results that were already present come
        back without an id and are dropped by the caller.
        """
        if self._client is None or not results:
            return results

        payload = [
            {
                "search_id": str(r.search_id) if r.search_id else None,
                "user_id": r.user_id,
                "mode": r.mode.value,
                "url": r.url,
                "url_hash": r.url_hash,
                "title": r.title,
                "summary": r.summary,
                "score": r.score,
                "status": ResultStatus.SENT.value,
                "raw": r.raw,
                "content": r.content,
            }
            for r in results
        ]

        rows = await self._execute(
            "save_results",
            lambda: self._table("results").upsert(
                payload,
                on_conflict="user_id,url_hash",
                ignore_duplicates=True,
            ),
        )
        if rows is None:
            return results

        by_hash = {str(row["url_hash"]): row for row in rows if row.get("url_hash")}
        stored: list[StoredResult] = []
        for result in results:
            row = by_hash.get(result.url_hash)
            if row is None:
                # Already stored from an earlier search; the caller decides
                # whether to re-send it.
                log.debug("supabase.result.duplicate", url_hash=result.url_hash[:12])
                continue
            stored.append(
                result.model_copy(
                    update={
                        "id": UUID(str(row["id"])),
                        "status": ResultStatus.SENT,
                        "created_at": _parse_timestamp(row.get("created_at")),
                    }
                )
            )
        return stored

    async def get_result(self, result_id: UUID) -> StoredResult | None:
        """Load one result, for the 'Подробнее' button."""
        if self._client is None:
            return None
        rows = await self._execute(
            "get_result",
            lambda: self._table("results").select("*").eq("id", str(result_id)).limit(1),
        )
        if not rows:
            return None
        return _row_to_result(rows[0])

    async def set_result_status(self, result_id: UUID, status: ResultStatus) -> None:
        if self._client is None:
            return
        await self._execute(
            "set_result_status",
            lambda: self._table("results")
            .update({"status": status.value})
            .eq("id", str(result_id)),
        )

    # -- feedback ----------------------------------------------------------

    async def record_feedback(self, *, result_id: UUID, user_id: int, action: Feedback) -> None:
        """Append a button press to the history and move the result's status."""
        if self._client is None:
            return
        await self._execute(
            "record_feedback",
            lambda: self._table("feedback").insert(
                {"result_id": str(result_id), "user_id": user_id, "action": action.value}
            ),
        )
        status = action.to_status()
        if status is not None:
            await self.set_result_status(result_id, status)

    async def list_saved(self, user_id: int, limit: int = 20) -> list[StoredResult]:
        """The user's saved results, newest first."""
        if self._client is None:
            return []
        rows = await self._execute(
            "list_saved",
            lambda: self._table("results")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", ResultStatus.SAVED.value)
            .order("created_at", desc=True)
            .limit(limit),
        )
        return [_row_to_result(row) for row in rows or []]

    # -- internals ---------------------------------------------------------

    def _table(self, name: str):  # type: ignore[no-untyped-def]
        assert self._client is not None
        return self._client.table(name)

    async def _execute(self, operation: str, build):  # type: ignore[no-untyped-def]
        """Run a PostgREST query, logging and swallowing failures.

        Returns the response rows, or ``None`` when the call failed. Storage is
        never allowed to take down a request that has already done real work.
        """
        try:
            response = await build().execute()
        except APIError as exc:
            log.error(
                "supabase.query.failed",
                operation=operation,
                code=exc.code,
                message=exc.message,
                hint=exc.hint,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - network, DNS, timeouts
            # No traceback: a Supabase outage would otherwise write one per
            # query, and the type plus message already identifies the failure.
            log.error(
                "supabase.query.error",
                operation=operation,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None
        return response.data or []


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _parse_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_to_result(row: dict[str, Any]) -> StoredResult:
    """Build a :class:`StoredResult` from a PostgREST row."""
    return StoredResult(
        id=UUID(str(row["id"])) if row.get("id") else None,
        search_id=UUID(str(row["search_id"])) if row.get("search_id") else None,
        user_id=int(row["user_id"]),
        mode=Mode(row["mode"]),
        url=row["url"],
        url_hash=row["url_hash"],
        title=row.get("title") or "",
        summary=row.get("summary") or "",
        score=int(row.get("score") or 0),
        status=ResultStatus(row.get("status") or ResultStatus.NEW.value),
        raw=row.get("raw") or {},
        content=row.get("content"),
        created_at=_parse_timestamp(row.get("created_at")),
    )
