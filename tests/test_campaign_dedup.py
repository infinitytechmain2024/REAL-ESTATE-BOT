"""Same property on several sites: pure dedup rules, the runner's one card, and migration 036 on PostgreSQL."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from bot.analysis_pipeline.cards import also_on, render_card
from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import Listing, listing_of, object_key, same_object
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from tests.test_campaign_runner import FakeMessenger

IDEALISTA = "https://www.idealista.com/inmueble/1/"
FOTOCASA = "https://www.fotocasa.es/es/comprar/vivienda/valencia/2/"


def flat(price: float | None = 199_000, area: float | None = 85, rooms: int | None = 3,
         location: str = "Ruzafa, Valencia", **kw) -> Listing:
    return Listing(price=price, area=area, rooms=rooms, location=location, deal="sale", currency="EUR", **kw)


def test_same_flat_on_two_sites_is_one_object() -> None:
    a = flat(199_000, 85, location="Ruzafa, Valencia", title="Piso luminoso en Ruzafa")
    b = flat(200_000, 86, location="Barrio de Ruzafa, Valencia", title="Vendo piso reformado")
    assert same_object(a, b) and same_object(b, a)
    assert object_key(a) is not None


def test_different_rooms_price_area_deal_or_district_are_not_the_same() -> None:
    base = flat()
    assert not same_object(base, flat(rooms=2))
    assert not same_object(base, flat(price=210_000))
    assert not same_object(base, flat(area=95))
    assert not same_object(base, flat(location="Benimaclet, Valencia"))  # same price and size, other district
    # the same district alone is not enough once both state a street address
    assert not same_object(flat(address="Calle Cuba 3", district="Ruzafa"), flat(address="Calle Sueca 9", district="Ruzafa"))
    assert same_object(flat(address="Calle Cuba 3", district="Ruzafa"), flat(address="C/ Cuba 3, bajo", district="Ruzafa"))
    assert not same_object(base, Listing(price=199_000, area=85, rooms=3, location="Ruzafa", deal="rent", currency="EUR"))


def test_a_city_alone_is_not_a_location() -> None:
    assert not same_object(flat(location="Valencia"), flat(location="Valencia, España"))
    # ... unless the titles are long and almost identical
    title = "Piso de tres habitaciones con terraza y garaje incluido en venta"
    assert same_object(flat(location="Valencia", title=title), flat(location="Valencia", title=title + " "))


def test_an_unknown_dimension_needs_rooms_and_location_to_agree() -> None:
    assert same_object(flat(area=None), flat(area=86))
    assert not same_object(flat(area=None, rooms=None), flat(area=86))
    assert not same_object(flat(price=None, area=None), flat())  # nothing to compare
    assert not same_object(flat(area=None, location="Valencia", title="a b c"), flat(area=86, location="Valencia"))


def test_other_floor_or_house_number_is_another_flat() -> None:
    a = flat(address="Calle Colón 5", floor=2, url=IDEALISTA)
    assert not same_object(a, flat(address="Calle Colón 5", floor=5, url=FOTOCASA))  # one building, two flats
    assert same_object(a, flat(address="Calle Colón 5", floor=2, url=FOTOCASA))
    assert not same_object(flat(address="Calle Colón 5", url=IDEALISTA), flat(address="Calle Colón 120", url=FOTOCASA))


def test_two_ads_of_one_host_need_the_same_address_and_floor() -> None:
    one, two = "https://www.idealista.com/inmueble/1/", "https://www.idealista.com/inmueble/2/"
    assert not same_object(flat(url=one), flat(url=two))  # same price and area, no address
    assert not same_object(flat(address="Calle Cuba 3", url=one), flat(address="Calle Cuba 3", floor=2, url=two, ))
    assert same_object(flat(address="Calle Cuba 3", floor=2, url=one), flat(address="Calle Cuba 3", floor=2, url=two))
    assert same_object(flat(address="Calle Cuba 3", url=one), flat(address="Calle Cuba 3", url=two))
    assert same_object(flat(address="Calle Cuba 3", url=IDEALISTA), flat(address="Calle Cuba 3", url=FOTOCASA))


def test_titles_alone_merge_only_long_titles_with_price_and_area() -> None:
    long = "Piso reformado de tres habitaciones con terraza ascensor y garaje incluido"
    assert same_object(flat(location="Valencia", title=long), flat(location="Valencia", title=long))
    assert not same_object(flat(location="Valencia", title=long, area=None), flat(location="Valencia", title=long, area=None))
    assert not same_object(flat(location="Valencia", title="Piso con terraza y garaje"),
                           flat(location="Valencia", title="Piso con terraza y garaje"))


def test_object_key_needs_a_price_or_an_area() -> None:
    assert object_key(flat(price=None, area=None)) is None
    assert object_key(flat(price=None)) is not None
    assert object_key(flat(199_000)) == object_key(flat(199_100))


def test_listing_of_reads_the_payload() -> None:
    got = listing_of({"price_amount": 199000, "price_currency": "eur", "area_m2": 85.0, "rooms": 3,
                      "location": "Ruzafa", "deal_type": "sale", "summary_ru": "Светлая квартира", "floor": 4,
                      "address": "Calle Cuba 3", "district": "Ruzafa"}, url=IDEALISTA)
    assert (got.floor, got.address, got.district) == (4, "Calle Cuba 3", "Ruzafa")
    assert (got.price, got.currency, got.area, got.rooms, got.deal, got.url) == (199000.0, "EUR", 85.0, 3, "sale", IDEALISTA)


def test_card_shows_also_on_line() -> None:
    payload = {"price_amount": 199000, "price_currency": "EUR", "location": "Ruzafa", "original_post_link": IDEALISTA}
    links = [{"url": FOTOCASA, "site": "fotocasa.es"}]
    card = render_card(payload, cluster_links=links)
    assert "Также на: Fotocasa" in card and FOTOCASA in card
    assert "Также на" not in render_card(payload)
    assert also_on([]) is None


# --- runner -------------------------------------------------------------------------------------

GOAL = "Купить квартиру в Валенсии до 250 000 €"


def payload(price: float, area: float, rooms: int = 3, location: str = "Ruzafa, Valencia", link: str = IDEALISTA) -> dict:
    return {"price_amount": price, "price_currency": "EUR", "area_m2": area, "rooms": rooms, "location": location,
            "deal_type": "sale", "original_post_link": link, "summary_ru": "Квартира"}


async def setup():
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7},
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL, actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    return store, messenger, runner, cid


def add(store: MemoryRunStore, cid: str, fid: str, link: str, **kw) -> None:
    store.add_finding(cid, fid, f"🏠 {fid}", payload=payload(link=link, **kw), original=f"post {fid}",
                      vertical="real_estate", url=link)


async def test_second_sighting_is_attached_and_the_head_card_is_edited() -> None:
    store, messenger, runner, cid = await setup()
    add(store, cid, "f1", IDEALISTA, price=199_000, area=85)
    add(store, cid, "f2", FOTOCASA, price=200_000, area=86)
    await runner.step(cid)
    assert len(messenger.findings()) == 1
    assert messenger.edits and messenger.edits[-1][1] == 1
    edited = messenger.edits[-1][2]
    assert "Также на: Fotocasa" in edited and FOTOCASA in edited and edited.rstrip().endswith("🔎 Найдено: 1 · ищу дальше")
    assert store.duplicates == {"f2": "f1"} and store.cluster_links["f1"] == [{"url": FOTOCASA, "site": "fotocasa.es"}]
    assert await store.streamed_count(cid) == 1
    # a restart or another tick neither re-sends nor re-edits
    edits = len(messenger.edits)
    await runner.step(cid)
    assert len(messenger.findings()) == 1 and len(messenger.edits) == edits


async def test_different_objects_get_their_own_cards() -> None:
    store, messenger, runner, cid = await setup()
    add(store, cid, "f1", IDEALISTA, price=199_000, area=85)
    add(store, cid, "f2", FOTOCASA, price=199_000, area=85, rooms=2)
    add(store, cid, "f3", "https://www.pisos.com/3", price=199_000, area=85, location="Benimaclet, Valencia")
    await runner.step(cid)
    assert len(messenger.findings()) == 3 and store.duplicates == {}


async def test_a_failed_edit_keeps_the_attachment_and_sends_nothing() -> None:
    store, messenger, runner, cid = await setup()
    add(store, cid, "f1", IDEALISTA, price=199_000, area=85)
    await runner.step(cid)
    messenger.gone = True
    add(store, cid, "f2", FOTOCASA, price=199_500, area=85)
    await runner.step(cid)
    assert len(messenger.findings()) == 1 and store.duplicates == {"f2": "f1"} and not messenger.edits


async def test_the_head_keeps_its_card_number_when_a_held_card_was_sent_before_it() -> None:
    store, messenger, runner, cid = await setup()
    add(store, cid, "f1", IDEALISTA, price=199_000, area=85)
    await runner.step(cid)  # card 1
    await store.hold_finding(cid, "fs", "similar", 0.1)
    assert await store.claim_held(cid, "fs") == 2  # an approved similar card is card 2
    await store.finding_sent("fs", 99)
    other = "https://www.pisos.com/9"
    add(store, cid, "f2", other, price=150_000, area=60, location="Benimaclet, Valencia")
    await runner.step(cid)  # card 3: the exact cards alone would number it 2
    add(store, cid, "f3", FOTOCASA, price=151_000, area=60, location="Benimaclet, Valencia")
    await runner.step(cid)
    assert store.duplicates == {"f3": "f2"}
    assert messenger.edits[-1][2].rstrip().endswith("🔎 Найдено: 3 · ищу дальше")


async def test_a_duplicate_is_recorded_as_excluded_and_delivered() -> None:
    from bot.agents.recorder import MemoryRecorder

    store, messenger, _, cid = await setup()
    recorder = MemoryRecorder()
    runner = CampaignRunner(store.campaigns, store, messenger, None, owner_ids={7}, recorder=recorder,  # type: ignore[arg-type]
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0))
    add(store, cid, "f1", IDEALISTA, price=199_000, area=85)
    add(store, cid, "f2", FOTOCASA, price=200_000, area=86)
    await runner.step(cid)
    dup = recorder.rows[(cid, "f2")]
    assert (dup.state, dup.bucket, dup.reason) == ("excluded", "excluded", "duplicate_of:f1")
    assert recorder.rows[(cid, "f1")].state == "sent" and store.findings_state["f2"] == "delivered"


async def test_the_heads_are_fetched_once_per_step_and_the_head_text_only_for_an_edit() -> None:
    store, _, runner, cid = await setup()
    calls: list[str] = []
    original = store.recent_sent_findings

    async def counting(campaign_id: str, limit: int = 200):
        calls.append(campaign_id)
        return await original(campaign_id, limit)

    store.recent_sent_findings = counting  # type: ignore[method-assign]
    add(store, cid, "f1", IDEALISTA, price=199_000, area=85)
    await runner.step(cid)
    assert len(calls) == 1 and store.finding_reads == []  # nothing to merge: no head text loaded
    for n in range(4):  # four sightings of other objects and one more of the first, in one step
        add(store, cid, f"o{n}", f"https://www.pisos.com/{n}", price=100_000 + n * 50_000, area=50 + n * 20,
            location=f"Barrio{n} Valencia", rooms=n + 1)
    add(store, cid, "f2", FOTOCASA, price=200_000, area=86)
    calls.clear()
    await runner.step(cid)
    assert len(calls) == 1 and store.duplicates == {"f2": "f1"} and store.finding_reads == ["f1"]


async def test_a_card_sent_in_this_step_is_a_head_for_the_next_findings() -> None:
    store, messenger, runner, cid = await setup()
    add(store, cid, "f1", IDEALISTA, price=199_000, area=85)
    await runner.step(cid)  # loads the index (empty), sends f1 ... in a later step f2 and f3 arrive together
    add(store, cid, "f2", "https://www.pisos.com/2", price=150_000, area=60, location="Benimaclet, Valencia")
    add(store, cid, "f3", FOTOCASA, price=151_000, area=60, location="Benimaclet, Valencia")
    await runner.step(cid)
    assert store.duplicates == {"f3": "f2"} and len(messenger.findings()) == 2


async def test_a_finding_that_is_already_sent_or_held_is_never_attached() -> None:
    store, _, runner, cid = await setup()
    add(store, cid, "f1", IDEALISTA, price=199_000, area=85)
    await runner.step(cid)
    await store.hold_finding(cid, "h", "similar", 0.1)
    assert await store.attach_to_cluster("h", "f1", {"url": FOTOCASA, "site": "fotocasa.es"}) is None
    assert "f1" not in store.cluster_links or store.cluster_links["f1"] == []
    assert store.duplicates == {}


# --- PostgreSQL (migration 036) -----------------------------------------------------------------

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


async def _seed(pool, cid: str, items: list[tuple[str, dict]]) -> list[str]:
    ids: list[str] = []
    async with pool.acquire() as conn:
        source = await conn.fetchval(
            """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, acquisition_method, state)
               values ('facebook', 'group', 'real_estate', 'https://www.facebook.com/groups/pisos/', 'facebook_connector',
                       'active') returning id""")
        batch = await conn.fetchval(
            """insert into acquisition_batches (platform, acquisition_method, vertical, state, max_items, campaign_id)
               values ('facebook', 'facebook_connector', 'real_estate', 'succeeded', 1, $1::uuid) returning id""", cid)
        item = await conn.fetchval(
            "insert into acquisition_batch_items (batch_id, source_id, sequence_no, state) values ($1, $2, 1, 'succeeded') returning id",
            batch, source)
        run = await conn.fetchval(
            "insert into acquisition_runs (source_id, batch_item_id, state, acquisition_method) values ($1, $2, 'succeeded', 'facebook_connector') returning id",
            source, item)
        for n, (url, data) in enumerate(items):
            post = await conn.fetchval(
                """insert into collected_posts (acquisition_run_id, source_id, platform_post_id, canonical_url, body_text,
                                                state, content_hash)
                   values ($1, $2, $3, $4, 'piso', 'analysed', $5) returning id""",
                run, source, str(n), url, uuid.uuid4().hex)
            ids.append(str(await conn.fetchval(
                """insert into findings (post_id, source_id, vertical, finding_type, state, structured_payload, confidence,
                                         dedupe_key)
                   values ($1, $2, 'real_estate', 'real_estate_proposition', 'ready', $3::jsonb, 0.9, $4) returning id""",
                post, source, json.dumps(data), uuid.uuid4().hex)))
    return ids


@needs_db
async def test_postgres_clusters_duplicates_and_streams_one_card(pool) -> None:
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.store import PostgresCampaignStore
    from bot.orchestra.store import SafetyLimits

    columns = {r["column_name"]: r for r in await pool.fetch(
        "select column_name, data_type, column_default from information_schema.columns "
        "where table_name = 'campaign_findings' and column_name in ('cluster_id', 'cluster_links', 'duplicate_of')")}
    assert set(columns) == {"cluster_id", "cluster_links", "duplicate_of"}
    assert columns["cluster_links"]["data_type"] == "jsonb" and "'[]'" in columns["cluster_links"]["column_default"]
    assert await pool.fetchval("select count(*) from pg_indexes where indexname = 'campaign_findings_cluster_idx'") == 1

    campaigns = PostgresCampaignStore(pool)
    store = PostgresRunStore(pool, SafetyLimits())
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7},
                            config=RunnerConfig(relevance_fail_closed=False))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL, actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    head, dup, other = await _seed(pool, cid, [
        (IDEALISTA, payload(199_000, 85)), (FOTOCASA, payload(200_000, 86, link=FOTOCASA)),
        ("https://www.pisos.com/9", payload(199_000, 85, rooms=1))])

    await runner.step(cid)
    assert len(messenger.findings()) == 2 and len(messenger.edits) == 1
    assert "Также на: Fotocasa" in messenger.edits[0][2] and messenger.edits[0][2].endswith("🔎 Найдено: 1 · ищу дальше")
    rows = {r["finding_id"]: r for r in await pool.fetch(
        "select finding_id::text, state, cluster_id::text, duplicate_of::text, cluster_links::text as links from campaign_findings")}
    assert rows[head]["state"] == "sent" and rows[head]["cluster_id"] == head
    assert json.loads(rows[head]["links"]) == [{"url": FOTOCASA, "site": "fotocasa.es"}]
    assert (rows[dup]["state"], rows[dup]["duplicate_of"], rows[dup]["cluster_id"]) == ("duplicate", head, head)
    assert rows[other]["state"] == "sent" and rows[other]["cluster_id"] is None
    assert await store.streamed_count(cid) == 2

    sent = {s.finding.id: s for s in await store.recent_sent_findings(cid)}
    assert set(sent) == {head, other} and sent[head].links[0]["url"] == FOTOCASA and sent[head].cluster_id == head
    assert await store.claim_finding(cid, dup) is None  # a duplicate is never picked up again
    again = await store.attach_to_cluster(dup, head, {"url": FOTOCASA, "site": "fotocasa.es"})
    assert again is not None and len(again.links) == 1  # the same link is added once
    assert await store.attach_to_cluster(dup, other, {}) is not None
    assert await store.attach_to_cluster(dup, dup, {"url": "x"}) is None  # a duplicate is not a head


@needs_db
async def test_postgres_stores_the_card_number_and_attach_rolls_back_for_a_sent_finding(pool) -> None:
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.store import PostgresCampaignStore
    from bot.orchestra.store import SafetyLimits

    campaigns = PostgresCampaignStore(pool)
    store = PostgresRunStore(pool, SafetyLimits())
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL, actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    head, similar, dup, other, late = await _seed(pool, cid, [
        (IDEALISTA, payload(199_000, 85)), ("https://www.pisos.com/s", payload(150_000, 60, rooms=2)),
        (FOTOCASA, payload(200_000, 86, link=FOTOCASA)), ("https://www.pisos.com/o", payload(310_000, 120, rooms=4)),
        ("https://www.habitaclia.com/l", payload(400_000, 160, rooms=5))])

    assert await store.claim_finding(cid, head) == 1
    await store.finding_sent(head, 11)
    assert await store.hold_finding(cid, similar, "similar", 0.1)
    assert await store.claim_held(cid, similar) == 2  # the approved similar card is card 2 ...
    await store.finding_sent(similar, 12)
    assert await store.claim_finding(cid, other) == 3  # ... so the next exact card is 3, not 2
    await store.finding_sent(other, 13)
    numbers = {r["finding_id"]: r["card_number"] for r in await pool.fetch(
        "select finding_id::text, card_number from campaign_findings where state = 'sent'")}
    assert numbers == {head: 1, similar: 2, other: 3}
    sent = {s.finding.id: s for s in await store.recent_sent_findings(cid)}
    assert sent[other].number == 3 and sent[head].number == 1 and sent[other].finding.original == ""
    await pool.execute("update campaign_findings set card_number = null where finding_id = $1::uuid", other)
    assert {s.finding.id: s.number for s in await store.recent_sent_findings(cid)}[other] == 2  # the old computation
    assert (await store.stream_finding(other)).original == "piso"  # the lazily fetched head has the post text
    assert await store.stream_finding(str(uuid.uuid4())) is None

    # A duplicate leaves ``ready`` like a sent finding does.
    assert await pool.fetchval("select state from findings where id = $1::uuid", dup) == "ready"
    attached = await store.attach_to_cluster(dup, head, {"url": FOTOCASA, "site": "fotocasa.es"})
    assert attached is not None and attached.message_id == 11
    assert await pool.fetchval("select state from findings where id = $1::uuid", dup) == "delivered"
    # A finding that was sent meanwhile is no duplicate: nothing is written, the head's links stay.
    assert await store.claim_finding(cid, late) == 4
    await store.finding_sent(late, 14)
    assert await store.attach_to_cluster(late, head, {"url": "https://x.es/late", "site": "x.es"}) is None
    row = await pool.fetchrow("select state, duplicate_of from campaign_findings where finding_id = $1::uuid", late)
    assert (row["state"], row["duplicate_of"]) == ("sent", None)
    links = json.loads(await pool.fetchval("select cluster_links::text from campaign_findings where finding_id = $1::uuid", head))
    assert links == [{"url": FOTOCASA, "site": "fotocasa.es"}]
