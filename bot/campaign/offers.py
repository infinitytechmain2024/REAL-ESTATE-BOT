"""The «show similar / farther options?» question and its answer (migration 019).

The campaign runner holds the findings that are not an exact match
(``tolerance``) and asks the requester once per bucket, with the buttons
«Одобрить» / «Нет». The Telegram control plane receives the button press and
records the answer on ``campaign_offers``; the runner reads it on its next tick
and streams the held cards (approved) or never sends them (declined). The two
processes share nothing but PostgreSQL.

When the runner asks
--------------------
* similar: once, as soon as at least one similar listing is held and no exact
  one was found so far, or when the search finishes with similar ones held.
* other: once, after the search has finished, when such listings are held and
  the similar question (if any) has been answered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    import asyncpg

OfferBucket = Literal["similar", "other"]
Decision = Literal["approved", "declined", "already", "forbidden", "unknown"]

CALLBACK_KIND = "near"
APPROVE, DECLINE = "Одобрить", "Нет"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

REPLIES: dict[str, str] = {
    "approved:similar": "Хорошо, присылаю похожие варианты.",
    "approved:other": "Хорошо, присылаю более далёкие варианты.",
    "declined:similar": "Хорошо, похожие варианты не присылаю.",
    "declined:other": "Хорошо, более далёкие варианты не присылаю.",
    "already": "Ответ уже учтён.",
    "forbidden": "Ответить может только тот, кто дал задачу.",
    "unknown": "Эта кнопка больше не действует.",
}


@dataclass(frozen=True, slots=True)
class Deviation:
    """What the closest held listing has outside the criteria, for the «Одобрить» question.

    ``kind``: price | area | other. ``example`` / ``requested``: «~60 000 €» / «~50 000 €» (price),
    «~1 600 м²» / «от 2 000 м²» (area). ``phrase``: a short Russian phrase that completes
    «есть варианты …» («дальше от метро»), from the relevance check.
    """

    kind: str = "other"
    example: str | None = None
    requested: str | None = None
    phrase: str | None = None
    land: bool = False  # «участки» instead of «варианты»


def similar_question(deviation: Deviation | None = None, *, exact_found: bool) -> str:
    """The similar question says what is outside the criteria.

    «По вашим критериям пока ничего не нашёл, но есть варианты чуть дороже (например ~60 000 € при запросе
    ~50 000 €). Показать?» -- or, once exact results were sent, «Есть ещё похожие варианты (например …). Показать?»
    """
    lead, hint = "похожие варианты", None
    what = "участки" if deviation is not None and deviation.land else "варианты"
    if deviation is not None and deviation.kind == "price" and deviation.example and deviation.requested:
        lead, hint = f"{what} чуть дороже", f"например {deviation.example} при запросе {deviation.requested}"
    elif deviation is not None and deviation.kind == "area" and deviation.example and deviation.requested:
        lead, hint = f"{what} меньшей площади", f"{deviation.example} при запросе {deviation.requested}"
    elif deviation is not None and deviation.phrase:
        lead = f"{what} {deviation.phrase}"
    if exact_found:
        if deviation is not None and deviation.kind == "area" and hint:
            hint = f"например {hint}"
        elif hint is None and deviation is not None and deviation.phrase:
            hint = deviation.phrase
        return f"Есть ещё похожие варианты{f' ({hint})' if hint else ''}. Показать?"
    return f"По вашим критериям пока ничего не нашёл, но есть {lead}{f' ({hint})' if hint else ''}. Показать?"


OTHER_QUESTION = "Показать более далёкие варианты?"


def callback_data(approve: bool, bucket: OfferBucket, campaign_id: str) -> str:
    """``near:yes|no:similar|other:<campaign uuid>``, at most 53 bytes (Telegram allows 64)."""
    data = f"{CALLBACK_KIND}:{'yes' if approve else 'no'}:{bucket}:{campaign_id}"
    if len(data.encode()) > 64:
        raise ValueError("callback data longer than 64 bytes")
    return data


def buttons(bucket: OfferBucket, campaign_id: str) -> tuple[tuple[str, str], ...]:
    return ((APPROVE, callback_data(True, bucket, campaign_id)), (DECLINE, callback_data(False, bucket, campaign_id)))


def parse_callback(action: str, target: str) -> tuple[bool, OfferBucket, str] | None:
    """(approve, bucket, campaign id) from the ``action`` and ``target`` of a ``near:`` callback."""
    bucket, _, campaign_id = target.partition(":")
    if action not in ("yes", "no") or bucket not in ("similar", "other") or not _UUID.match(campaign_id):
        return None
    return action == "yes", bucket, campaign_id  # type: ignore[return-value]


def reply_for(decision: Decision, bucket: OfferBucket) -> str:
    return REPLIES.get(f"{decision}:{bucket}", REPLIES.get(decision, REPLIES["unknown"]))


class OfferDesk(Protocol):
    async def decide(self, campaign_id: str, bucket: OfferBucket, *, approve: bool, user_id: int,
                     owner: bool) -> Decision:
        """Record the answer to an open question; only the requester or an owner may answer, once."""
        ...


@dataclass
class MemoryOffer:
    requested_by: int
    state: str = "asked"
    decided_by: int | None = None
    message_id: int | None = None


@dataclass
class MemoryOfferDesk:
    offers: dict[tuple[str, str], MemoryOffer] = field(default_factory=dict)

    async def decide(self, campaign_id: str, bucket: OfferBucket, *, approve: bool, user_id: int,
                     owner: bool) -> Decision:
        offer = self.offers.get((campaign_id, bucket))
        if offer is None:
            return "unknown"
        if not owner and offer.requested_by != user_id:
            return "forbidden"
        if offer.state != "asked":
            return "already"
        offer.state, offer.decided_by = ("approved" if approve else "declined"), user_id
        return offer.state  # type: ignore[return-value]


class PostgresOfferDesk:
    """The control plane's side: shares the Orchestra's pool."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def decide(self, campaign_id: str, bucket: OfferBucket, *, approve: bool, user_id: int,
                     owner: bool) -> Decision:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """select o.state, c.requested_by from campaign_offers o join campaigns c on c.id = o.campaign_id
                    where o.campaign_id = $1::uuid and o.bucket = $2 for update of o""",
                campaign_id, bucket,
            )
            if row is None:
                return "unknown"
            if not owner and row["requested_by"] != user_id:
                return "forbidden"
            if row["state"] != "asked":
                return "already"
            state = "approved" if approve else "declined"
            await conn.execute(
                """update campaign_offers set state = $3, decided_by = $4, decided_at = now()
                    where campaign_id = $1::uuid and bucket = $2 and state = 'asked'""",
                campaign_id, bucket, state, user_id,
            )
            return state  # type: ignore[return-value]
