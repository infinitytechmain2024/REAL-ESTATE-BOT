"""SA-3 Recorder: every finding a campaign sends, holds or excludes is stored before anything is sent.

The real-time path is unchanged (``CampaignRunner._send_card`` sends the same
card at the same moment); the Recorder only puts an outbox row in front of it:

1. ``to_send`` before the Telegram call (if this write fails, nothing is sent
   and the finding is picked up again on the next tick);
2. ``sent`` with the Telegram message id once the card is out;
3. a failed send stays ``to_send`` with ``send_attempts`` + 1.

Held (similar/other) and excluded findings are stored too, so the later
Deduplication & Final Analysis sub-agent (SA-4) and the review package (SA-5)
see the whole run. Each row carries what de-duplication needs: the source
``site``, ``url_key``, a facts ``fingerprint`` and a 64-bit ``simhash`` of the text.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from bot.web_search.urls import url_key

State = Literal["to_send", "sent", "held", "excluded"]
AGENT = "campaign-runner"
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_FB_GROUP = re.compile(r"^/groups/([^/?#]+)")


@dataclass(frozen=True, slots=True)
class FindingRecord:
    campaign_id: str
    finding_id: str
    state: State
    bucket: str | None
    site: str
    url: str | None
    url_key: str
    fingerprint: str | None
    simhash: int | None
    facts: dict[str, Any] = field(default_factory=dict)
    card_text: str | None = None
    telegram_message_id: int | None = None
    send_attempts: int = 0
    agent: str = AGENT
    reason: str | None = None  # why it was excluded unsent, e.g. ``duplicate_of:<head finding id>`` (migration 039)


class Recorder(Protocol):
    async def to_send(self, record: FindingRecord) -> None: ...
    async def sent(self, campaign_id: str, finding_id: str, message_id: int) -> None: ...
    async def send_failed(self, campaign_id: str, finding_id: str) -> None: ...
    async def held(self, record: FindingRecord) -> None: ...
    async def excluded(self, record: FindingRecord) -> None: ...


# -- what a row holds --


def record_of(campaign_id: str, finding_id: str, *, state: State, bucket: str | None,
              payload: dict[str, Any] | None, text: str, card_text: str | None = None,
              reason: str | None = None) -> FindingRecord:
    payload = payload or {}
    link = str(payload.get("original_post_link") or payload.get("url") or "").strip() or None
    return FindingRecord(
        campaign_id=campaign_id, finding_id=finding_id, state=state, bucket=bucket,
        site=site_of(link) if link else "unknown", url=link,
        url_key=url_key(link) if link else f"finding:{finding_id}",
        fingerprint=fingerprint(payload), simhash=simhash64(text), facts=facts_of(payload), card_text=card_text,
        reason=reason[:120] if reason else None,
    )


def site_of(url: str) -> str:
    """The source a duplicate is judged by: the host without www./m.; a Facebook group counts as its own site."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host in ("facebook.com", "fb.com"):
        group = _FB_GROUP.match(parts.path)
        if group:
            return f"facebook.com/groups/{group.group(1).lower()}"
    return host or "unknown"


def facts_of(payload: dict[str, Any]) -> dict[str, Any]:
    keys = ("price_amount", "price_currency", "area_m2", "rooms", "deal_type", "property_type", "location", "country",
            "listing_kind", "source_language")
    return {k: payload[k] for k in keys if payload.get(k) not in (None, "", [])}


def fingerprint(payload: dict[str, Any]) -> str | None:
    """deal|type|price to 1 000|area to 10 m²|locality; None when neither price nor area is known."""
    price, area = _number(payload.get("price_amount")), _number(payload.get("area_m2"))
    if price is None and area is None:
        return None
    parts = [
        str(payload.get("deal_type") or ""),
        str(payload.get("property_type") or ""),
        "" if price is None else str(int(round(price, -3))),
        str(payload.get("price_currency") or "").upper(),
        "" if area is None else str(int(round(area, -1))),
        _slug(str(payload.get("location") or "").split(",")[0]),
    ]
    return "|".join(parts)


def simhash64(text: str) -> int | None:
    """64-bit SimHash over word pairs of the normalised text (URLs and phone numbers removed), signed for bigint."""
    words = _WORD.findall(_PHONE.sub(" ", _URL.sub(" ", _fold(text))))
    if len(words) < 3:
        return None
    weights = [0] * 64
    for pair in pairwise(words):
        value = int.from_bytes(hashlib.blake2b(" ".join(pair).encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if value >> bit & 1 else -1
    unsigned = sum(1 << bit for bit in range(64) if weights[bit] > 0)
    return unsigned - (1 << 64) if unsigned >= 1 << 63 else unsigned


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _slug(text: str) -> str:
    return "-".join(_WORD.findall(_fold(text)))[:80]


# -- stores --


class PostgresRecorder:
    """Writes ``agent_findings`` (migration 024)."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def to_send(self, record: FindingRecord) -> None:
        await self._upsert(replace(record, state="to_send"))

    async def held(self, record: FindingRecord) -> None:
        await self._upsert(replace(record, state="held"))

    async def excluded(self, record: FindingRecord) -> None:
        await self._upsert(replace(record, state="excluded"))

    async def sent(self, campaign_id: str, finding_id: str, message_id: int) -> None:
        await self.pool.execute(
            """update agent_findings set state = 'sent', telegram_message_id = $3, sent_at = now(), updated_at = now()
                where campaign_id = $1::uuid and finding_id = $2::uuid""",
            campaign_id, finding_id, message_id)

    async def send_failed(self, campaign_id: str, finding_id: str) -> None:
        await self.pool.execute(
            """update agent_findings set send_attempts = send_attempts + 1, updated_at = now()
                where campaign_id = $1::uuid and finding_id = $2::uuid and state = 'to_send'""",
            campaign_id, finding_id)

    async def _upsert(self, r: FindingRecord) -> None:
        # A row already sent is never moved back: the user has seen it.
        await self.pool.execute(
            """insert into agent_findings (campaign_id, finding_id, state, bucket, site, url, url_key, fingerprint,
                                           simhash, facts, card_text, agent, reason)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13)
               on conflict (campaign_id, finding_id) do update
                  set state = excluded.state, bucket = coalesce(excluded.bucket, agent_findings.bucket),
                      card_text = coalesce(excluded.card_text, agent_findings.card_text),
                      reason = excluded.reason, updated_at = now()
                where agent_findings.state <> 'sent'""",
            r.campaign_id, r.finding_id, r.state, r.bucket, r.site, r.url, r.url_key, r.fingerprint, r.simhash,
            json.dumps(r.facts, ensure_ascii=False), r.card_text, r.agent, r.reason)


class MemoryRecorder:
    """In-process twin of ``PostgresRecorder`` for tests; ``fail`` makes the next write raise."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], FindingRecord] = {}
        self.sent_at: dict[tuple[str, str], datetime] = {}
        self.fail = False

    async def to_send(self, record: FindingRecord) -> None:
        self._upsert(replace(record, state="to_send"))

    async def held(self, record: FindingRecord) -> None:
        self._upsert(replace(record, state="held"))

    async def excluded(self, record: FindingRecord) -> None:
        self._upsert(replace(record, state="excluded"))

    async def sent(self, campaign_id: str, finding_id: str, message_id: int) -> None:
        key = (campaign_id, finding_id)
        if key in self.rows:
            self.rows[key] = replace(self.rows[key], state="sent", telegram_message_id=message_id)
            self.sent_at[key] = datetime.now(UTC)

    async def send_failed(self, campaign_id: str, finding_id: str) -> None:
        key = (campaign_id, finding_id)
        row = self.rows.get(key)
        if row is not None and row.state == "to_send":
            self.rows[key] = replace(row, send_attempts=row.send_attempts + 1)

    def _upsert(self, record: FindingRecord) -> None:
        if self.fail:
            self.fail = False
            raise RuntimeError("recorder unavailable")
        key = (record.campaign_id, record.finding_id)
        old = self.rows.get(key)
        if old is not None and old.state == "sent":
            return
        if old is not None:
            record = replace(record, bucket=record.bucket or old.bucket, card_text=record.card_text or old.card_text,
                             send_attempts=old.send_attempts)
        self.rows[key] = record
