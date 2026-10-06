"""Exact / similar / other findings of a campaign and the «Одобрить» / «Нет» question."""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign import offers as near
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.tolerance import (
    BUDGET_TOLERANCE,
    SIMILAR_CEILING,
    Request,
    budget_match,
    classify,
    money,
    request_for,
)
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from tests.test_campaign_runner import FakeMessenger

GOAL = "Купить квартиру в Мадриде до 50 000 €"
OWNER, USER, STRANGER = 7, 42, 99
CHAT = 4242
MADRID_50K = Request(amount=50_000, deal="sale", location="Madrid")


def listing(price: float | None, *, currency: str | None = "EUR", deal: str | None = "sale",
            location: str | None = "Madrid, Centro") -> dict:
    return {"summary": "Квартира", "summary_ru": "Квартира на продажу", "price_amount": price, "price_currency": currency,
            "deal_type": deal, "property_type": "apartment", "rooms": 2, "location": location,
            "url": "https://www.facebook.com/groups/pisos/posts/1/", "category": "real_estate"}


# --- the policy -----------------------------------------------------------------------------


def test_policy_constants_are_ten_and_twenty_five_percent() -> None:
    assert (BUDGET_TOLERANCE, SIMILAR_CEILING) == (0.10, 0.25)


@pytest.mark.parametrize(("price", "bucket"), [
    (52_000, "exact"), (55_000, "exact"), (50_000, "exact"),
    (45_000, "exact"), (30_000, "exact"),  # cheaper than a maximum budget is fine
    (55_001, "similar"), (60_000, "similar"), (62_500, "similar"),
    (62_501, "other"), (90_000, "other"),
])
def test_budget_given_as_a_maximum(price: int, bucket: str) -> None:
    assert classify(listing(price), MADRID_50K).bucket == bucket


def test_a_target_amount_has_a_band_on_both_sides() -> None:
    assert budget_match(45_000, 50_000, is_max=False).bucket == "exact"
    assert budget_match(44_000, 50_000, is_max=False).bucket == "similar"
    assert budget_match(37_000, 50_000, is_max=False).bucket == "other"
    assert budget_match(60_000, 50_000, is_max=False).distance == pytest.approx(0.2)


def test_unknown_price_currency_deal_and_city() -> None:
    assert classify(listing(None), MADRID_50K) == classify(listing(None), MADRID_50K)
    assert classify(listing(None), MADRID_50K).bucket == "other"  # a budget was asked for
    assert classify(listing(None), Request(deal="sale", location="Madrid")).bucket == "exact"  # no budget
    # A Spanish campaign is priced in euros: another currency is another market.
    assert classify(listing(52_000, currency="USD"), MADRID_50K).bucket == "excluded"
    assert classify(listing(52_000, currency="USD"), Request(amount=50_000, deal="sale")).bucket == "other"
    assert classify(listing(52_000, currency="€"), MADRID_50K).bucket == "exact"
    assert classify(listing(52_000, currency=None), MADRID_50K).bucket == "exact"  # taken as the requested one
    assert classify(listing(52_000, deal="rent"), MADRID_50K).bucket == "excluded"
    assert classify(listing(52_000, deal=None), MADRID_50K).bucket == "exact"
    assert classify(listing(52_000, location="Barcelona, Gràcia"), MADRID_50K).bucket == "other"
    assert classify(listing(52_000, location="Centro, cerca del metro"), MADRID_50K).bucket == "exact"
    assert classify(listing(52_000, location=None), MADRID_50K).bucket == "exact"
    assert classify(listing(900_000), MADRID_50K, vertical="investors").bucket == "exact"
    assert classify(None, MADRID_50K).distance == math.inf


def test_request_comes_from_the_plan() -> None:
    plan = plan_campaign(GOAL)
    assert request_for(plan.constraints, location=plan.location, vertical=plan.vertical) == MADRID_50K
    assert request_for({"max_price": 9000}, vertical="investors").amount is None
    assert money(60_000) == "~60 000 €"


# --- the runner --------------------------------------------------------------------------------


class ButtonMessenger(FakeMessenger):
    def __init__(self) -> None:
        super().__init__()
        self.asks: list[tuple[int, str, tuple[tuple[str, str], ...]]] = []
        self.ask_fail = 0

    async def send_buttons(self, chat_id: int, text: str, buttons) -> int:
        if self.ask_fail:
            self.ask_fail -= 1
            raise RuntimeError("telegram unreachable")
        self.asks.append((chat_id, text, tuple(buttons)))
        self.sent.append((chat_id, len(self.sent) + 1, text))
        return len(self.sent)


async def setup(requested_by: int = USER):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={OWNER},
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=requested_by, source_text=GOAL,
                                 actor=f"telegram:{requested_by}")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)  # a window stays open, so the search is still running until the test ends it
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({OWNER}))
    control = ControlPlane(settings, MemoryControlPlaneStore(), None, _sink, offers=store.desk)
    return campaigns, store, messenger, runner, cid, control


async def _sink(envelope) -> None:
    return None


def add(store: MemoryRunStore, cid: str, fid: str, price: float | None, **kw) -> None:
    store.add_finding(cid, fid, f"🏠 {fid}", payload=listing(price, **kw), original=f"post {fid}", vertical="real_estate")


def cards(messenger: ButtonMessenger) -> list[str]:
    return messenger.findings()


async def press(control: ControlPlane, user: int, data: str) -> str:
    return (await control.handle_callback(user, data, chat_id=CHAT)).text


async def test_exact_listing_is_sent_at_once() -> None:
    _, store, messenger, runner, cid, _ = await setup()
    add(store, cid, "f52", 52_000)
    await runner.tick()
    assert len(cards(messenger)) == 1 and cards(messenger)[0].endswith("🔎 Найдено: 1 · ищу дальше")
    assert "52 000" in cards(messenger)[0]
    assert messenger.asks == []
    assert store.buckets["f52"][0] == "exact"


async def test_similar_listing_waits_for_approval_then_streams() -> None:
    campaigns, store, messenger, runner, cid, control = await setup()
    add(store, cid, "f60", 60_000)
    await runner.tick()
    assert cards(messenger) == []
    assert [(chat, text) for chat, text, _ in messenger.asks] == [(
        CHAT, "По вашим критериям пока ничего не нашёл, но есть варианты чуть дороже "
              "(например ~60 000 € при запросе ~50 000 €). Показать?")]
    buttons = messenger.asks[0][2]
    assert [label for label, _ in buttons] == ["Одобрить", "Нет"]
    assert [data for _, data in buttons] == [f"near:yes:similar:{cid}", f"near:no:similar:{cid}"]
    assert all(len(data.encode()) <= 64 for _, data in buttons)

    for _ in range(3):  # no answer yet: nothing is sent, nothing is asked twice
        await runner.tick()
    assert cards(messenger) == [] and len(messenger.asks) == 1

    assert await press(control, USER, buttons[0][1]) == "Хорошо, присылаю похожие варианты."
    await runner.tick()
    assert len(cards(messenger)) == 1 and "60 000" in cards(messenger)[0]
    assert cards(messenger)[0].endswith("🔎 Найдено: 1 · ищу дальше")
    # A later similar listing streams without a new question.
    add(store, cid, "f58", 58_000)
    await runner.tick()
    await runner.tick()
    assert len(cards(messenger)) == 2 and len(messenger.asks) == 1
    assert await press(control, USER, buttons[1][1]) == "Ответ уже учтён."
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.tick()
    assert len(cards(messenger)) == 2 and len(messenger.asks) == 1


async def test_declined_similar_listings_are_never_sent() -> None:
    campaigns, store, messenger, runner, cid, control = await setup()
    add(store, cid, "f60", 60_000)
    await runner.tick()
    assert await press(control, USER, messenger.asks[0][2][1][1]) == "Хорошо, похожие варианты не присылаю."
    add(store, cid, "f61", 61_000)
    for _ in range(3):
        await runner.tick()
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.tick()
    await runner.tick()
    assert cards(messenger) == [] and len(messenger.asks) == 1
    assert await press(control, USER, messenger.asks[0][2][0][1]) == "Ответ уже учтён."
    await runner.tick()
    assert cards(messenger) == []


async def test_far_listing_is_other_and_only_offered_after_the_search() -> None:
    campaigns, store, messenger, runner, cid, control = await setup()
    add(store, cid, "f90", 90_000)
    add(store, cid, "f52", 52_000)
    await runner.tick()
    await runner.tick()
    assert store.buckets["f90"][0] == "other"
    assert len(cards(messenger)) == 1 and "52 000" in cards(messenger)[0]
    assert messenger.asks == []  # not similar, and the search is still running
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.tick()
    assert [text for _, text, _ in messenger.asks] == ["Показать более далёкие варианты?"]
    other = messenger.asks[0][2]
    assert [data for _, data in other] == [f"near:yes:other:{cid}", f"near:no:other:{cid}"]
    assert await press(control, USER, other[0][1]) == "Хорошо, присылаю более далёкие варианты."
    await runner.tick()
    assert len(cards(messenger)) == 2 and "90 000" in cards(messenger)[1]
    assert cards(messenger)[1].endswith("🔎 Найдено: 2")


async def test_exact_found_first_asks_about_similar_at_the_end_then_about_other() -> None:
    campaigns, store, messenger, runner, cid, control = await setup()
    add(store, cid, "f52", 52_000)
    add(store, cid, "f62", 62_000)
    add(store, cid, "f59", 59_000)
    add(store, cid, "f95", 95_000)
    await runner.tick()
    assert len(cards(messenger)) == 1 and messenger.asks == []  # exact found: similar waits for the end
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.tick()
    await runner.tick()
    # The closest similar listing is the example; «other» waits for the similar answer.
    assert [text for _, text, _ in messenger.asks] == [
        "Есть ещё похожие варианты (например ~59 000 € при запросе ~50 000 €). Показать?"]
    await press(control, USER, messenger.asks[0][2][0][1])
    await runner.tick()
    new = cards(messenger)[1:]
    assert ["59 000" in new[0], "62 000" in new[1]] == [True, True]  # closest first
    assert [text for _, text, _ in messenger.asks][-1] == "Показать более далёкие варианты?"
    await press(control, USER, messenger.asks[1][2][1][1])  # «Нет»
    await runner.tick()
    assert len(cards(messenger)) == 3 and all("95 000" not in c for c in cards(messenger))


async def test_each_finding_is_in_one_bucket_and_sent_at_most_once() -> None:
    campaigns, store, messenger, runner, cid, control = await setup()
    add(store, cid, "f52", 52_000)
    add(store, cid, "f60", 60_000)
    await runner.tick()
    # A second filing never moves a finding or claims it twice.
    assert not await store.hold_finding(cid, "f52", "similar", 0.2)
    assert not await store.hold_finding(cid, "f60", "other", 0.9)
    assert await store.claim_finding(cid, "f60") is None
    assert await store.claim_held(cid, "f52") is None
    assert store.buckets == {"f52": ("exact", 0.0), "f60": ("similar", pytest.approx(0.2))}
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.tick()
    await press(control, USER, messenger.asks[0][2][0][1])
    messenger.fail = 1  # Telegram down for the first try: the held card goes back to held, not lost
    await runner.tick()
    assert len(cards(messenger)) == 1 and store.buckets["f60"][0] == "similar"
    for _ in range(3):
        await runner.tick()
    assert len(cards(messenger)) == 2 and len(set(cards(messenger))) == 2
    assert await store.streamed_count(cid) == 2


async def test_a_failed_question_is_asked_again_next_tick() -> None:
    _, store, messenger, runner, cid, _ = await setup()
    add(store, cid, "f60", 60_000)
    messenger.ask_fail = 1
    await runner.tick()
    assert messenger.asks == [] and await store.offer_state(cid, "similar") is None
    await runner.tick()
    assert len(messenger.asks) == 1 and await store.offer_state(cid, "similar") == "asked"


async def test_only_the_requester_or_an_owner_may_answer() -> None:
    _, store, messenger, runner, cid, control = await setup()
    add(store, cid, "f60", 60_000)
    await runner.tick()
    yes = messenger.asks[0][2][0][1]
    assert await press(control, STRANGER, yes) == "Ответить может только тот, кто дал задачу."
    assert await store.offer_state(cid, "similar") == "asked"
    assert await press(control, OWNER, yes) == "Хорошо, присылаю похожие варианты."
    assert store.desk.offers[(cid, "similar")].decided_by == OWNER
    assert await press(control, USER, f"near:yes:similar:{cid[:-1]}x") == "Эта кнопка больше не действует."
    assert await press(control, USER, f"near:maybe:similar:{cid}") == "Эта кнопка больше не действует."
    assert await press(control, USER, f"near:yes:other:{cid}") == "Эта кнопка больше не действует."  # never asked


async def test_no_budget_means_everything_is_exact() -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={OWNER},
                            config=RunnerConfig(relevance_fail_closed=False))
    cid = await campaigns.create(plan_campaign("Купить квартиру в Мадриде"), chat_id=CHAT, requested_by=USER,
                                 source_text="x", actor="telegram:42")
    add(store, cid, "a", 900_000)
    add(store, cid, "b", None)
    await runner.tick()
    assert len(cards(messenger)) == 2 and messenger.asks == []


def test_callback_data_fits_telegram_limit() -> None:
    cid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    for approve in (True, False):
        for bucket in ("similar", "other"):
            assert len(near.callback_data(approve, bucket, cid).encode()) <= 64
    assert near.parse_callback("yes", f"similar:{cid}") == (True, "similar", cid)
    assert near.parse_callback("no", f"other:{cid}") == (False, "other", cid)
    assert near.parse_callback("yes", f"exact:{cid}") is None


# --- PostgreSQL (migration 019) ------------------------------------------------------------------

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                              reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))


@pytest.fixture
async def pool():
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_findings(pool, cid: str, prices: dict[str, float | None]) -> dict[str, str]:
    """One campaign batch with one post and finding per price; returns name -> finding id."""
    import json
    import uuid

    ids: dict[str, str] = {}
    async with pool.acquire() as conn:
        source = await conn.fetchval(
            """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, acquisition_method, state)
               values ('facebook', 'group', 'real_estate', 'https://www.facebook.com/groups/pisos/', 'facebook_connector',
                       'active') returning id""")
        batch = await conn.fetchval(
            """insert into acquisition_batches (platform, acquisition_method, vertical, state, max_items, campaign_id)
               values ('facebook', 'facebook_connector', 'real_estate', 'succeeded', 1, $1::uuid) returning id""", cid)
        item = await conn.fetchval(
            """insert into acquisition_batch_items (batch_id, source_id, sequence_no, state)
               values ($1, $2, 1, 'succeeded') returning id""", batch, source)
        run = await conn.fetchval(
            """insert into acquisition_runs (source_id, batch_item_id, state, acquisition_method)
               values ($1, $2, 'succeeded', 'facebook_connector') returning id""", source, item)
        for n, (name, price) in enumerate(prices.items()):
            post = await conn.fetchval(
                """insert into collected_posts (acquisition_run_id, source_id, platform_post_id, canonical_url, body_text,
                                                state, content_hash)
                   values ($1, $2, $3, $4, $5, 'analysed', $6) returning id""",
                run, source, str(n), f"https://www.facebook.com/groups/pisos/posts/{n}/", f"Vendo piso {name}",
                uuid.uuid4().hex)
            ids[name] = str(await conn.fetchval(
                """insert into findings (post_id, source_id, vertical, finding_type, state, structured_payload, confidence,
                                         dedupe_key)
                   values ($1, $2, 'real_estate', 'real_estate_proposition', 'ready', $3::jsonb, 0.9, $4) returning id""",
                post, source, json.dumps(listing(price)), uuid.uuid4().hex))
    return ids


@needs_db
async def test_postgres_holds_similar_until_approved_and_answers_once(pool) -> None:
    import asyncpg

    from bot.campaign.offers import PostgresOfferDesk
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.store import PostgresCampaignStore
    from bot.orchestra.store import SafetyLimits

    campaigns = PostgresCampaignStore(pool)
    store = PostgresRunStore(pool, SafetyLimits())
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={OWNER})
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    ids = await _seed_findings(pool, cid, {"f52": 52_000, "f60": 60_000, "f90": 90_000})

    await runner.step(cid)
    assert len(cards(messenger)) == 1 and "52 000" in cards(messenger)[0]
    rows = dict(await pool.fetch("select finding_id::text, bucket || ':' || state from campaign_findings"))
    assert rows == {ids["f52"]: "exact:sent", ids["f60"]: "similar:held", ids["f90"]: "other:held"}
    # Nothing left to collect, so the search finished in this step: exact found, then the similar question.
    assert (await campaigns.get(cid)).state == "completed"
    assert [t for _, t, _ in messenger.asks] == [
        "Есть ещё похожие варианты (например ~60 000 € при запросе ~50 000 €). Показать?"]
    assert await pool.fetchval("select telegram_message_id from campaign_offers where bucket='similar'") is not None

    desk = PostgresOfferDesk(pool)
    control = ControlPlane(ControlPlaneSettings(telegram_token="t", database_url="postgresql://x",
                                                operator_user_ids=frozenset({OWNER})),
                           MemoryControlPlaneStore(), None, _sink, offers=desk)
    yes, no = (data for _, data in messenger.asks[0][2])
    assert await press(control, STRANGER, yes) == "Ответить может только тот, кто дал задачу."
    assert await press(control, USER, yes) == "Хорошо, присылаю похожие варианты."
    assert await press(control, USER, no) == "Ответ уже учтён."
    assert tuple(await pool.fetchrow("select state, decided_by from campaign_offers where bucket='similar'")) == (
        "approved", USER)

    await runner.step(cid)
    assert len(cards(messenger)) == 2 and cards(messenger)[1].endswith("🔎 Найдено: 2")
    assert [t for _, t, _ in messenger.asks][-1] == "Показать более далёкие варианты?"
    assert await press(control, USER, messenger.asks[-1][2][1][1]) == "Хорошо, более далёкие варианты не присылаю."
    for _ in range(2):
        await runner.step(cid)
    assert len(cards(messenger)) == 2
    rows = dict(await pool.fetch("select finding_id::text, bucket || ':' || state from campaign_findings"))
    assert rows == {ids["f52"]: "exact:sent", ids["f60"]: "similar:sent", ids["f90"]: "other:held"}
    assert await store.streamed_count(cid) == 2
    # A held exact finding is impossible, and a finding can never get a second row.
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("update campaign_findings set state='held' where finding_id=$1::uuid", ids["f52"])
    assert not await store.hold_finding(cid, ids["f52"], "similar", 0.2)
    assert await store.claim_finding(cid, ids["f90"]) is None


async def test_a_rental_for_a_purchase_is_never_offered_or_sent() -> None:
    campaigns, store, messenger, runner, cid, control = await setup()
    add(store, cid, "rent", 900, deal="rent")
    add(store, cid, "f90", 90_000)
    await runner.tick()
    assert store.buckets["rent"][0] == "excluded"
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.tick()
    assert [text for _, text, _ in messenger.asks] == ["Показать более далёкие варианты?"]
    await press(control, USER, messenger.asks[0][2][0][1])
    for _ in range(3):
        await runner.tick()
    assert len(cards(messenger)) == 1 and "90 000" in cards(messenger)[0]
    assert not any("rent" in card for card in cards(messenger))


def test_unknown_price_with_a_budget_is_other() -> None:
    assert classify(listing(None), MADRID_50K).bucket == "other"


def test_unknown_area_against_a_minimum_is_similar_and_known_area_is_unchanged() -> None:
    request = Request(amount=None, deal="sale", location="Madrid", min_area=2000)
    assert classify({**listing(None), "area_m2": None}, request).bucket == "similar"
    assert classify(listing(None), request).bucket == "similar"
    assert classify({**listing(None), "area_m2": 2500}, request).bucket == "exact"
    assert classify(listing(None), Request(deal="sale", location="Madrid")).bucket == "exact"


def test_rooms_lower_than_requested_is_other_unknown_or_more_is_unchanged() -> None:
    request = Request(deal="sale", location="Madrid", rooms=3)
    assert classify({**listing(None), "rooms": 2}, request).bucket == "other"
    assert classify({**listing(None), "rooms": 3}, request).bucket == "exact"
    assert classify({**listing(None), "rooms": 4}, request).bucket == "exact"
    assert classify({**listing(None), "rooms": None}, request).bucket == "exact"
