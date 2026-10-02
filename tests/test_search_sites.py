"""The person approves the search sites before the web stage reads them, and the decisions are kept."""

from __future__ import annotations

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.sites import (
    EDIT_HINT,
    MAX_MESSAGE,
    QUESTION,
    REMINDER,
    Answer,
    GateConfig,
    MemorySiteStore,
    PostgresSiteStore,
    Site,
    SiteDesk,
    SiteGate,
    apply_answer,
    buttons,
    parse_answer,
    question_messages,
)
from bot.control_plane.models import IncomingMessage
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.web_search.store import MemoryWebStore, PostgresWebStore
from bot.web_search.worker import WebSearchConfig, WebSearchWorker
from tests.test_campaign_runner import Clock
from tests.test_near_match import ButtonMessenger, needs_db
from tests.test_near_match import pool as pool
from tests.test_web_search import FakeFetcher, FakeSearcher, ListGenerator

OWNER = 7
GOAL = "квартиры в аренду в Мадриде"
SITES = (Site("idealista.com", number=1, query="pisos Madrid"), Site("fotocasa.es", number=2, query="pisos Madrid"),
         Site("olx.ua", number=3, query="квартира Мадрид"), Site("pisos.com", number=4, query="квартира Мадрид"),
         Site("habitaclia.com", number=5, query="квартира Мадрид"))


# --- the answer ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "kind", "hosts"), [
    ("да", "all", set()),
    ("Все", "all", set()),
    ("так, всі", "all", set()),
    ("ок", "all", set()),
    ("кроме 3 и 5", "reject", {"olx.ua", "habitaclia.com"}),
    ("крім 3 і 5", "reject", {"olx.ua", "habitaclia.com"}),
    ("3, 5", "reject", {"olx.ua", "habitaclia.com"}),
    ("2-4", "reject", {"fotocasa.es", "olx.ua", "pisos.com"}),
    ("убери olx", "reject", {"olx.ua"}),
    ("прибери olx.ua", "reject", {"olx.ua"}),
    ("без fotocasa и 4", "reject", {"fotocasa.es", "pisos.com"}),
    ("только 1, 2", "only", {"idealista.com", "fotocasa.es"}),
    ("тільки idealista", "only", {"idealista.com"}),
])
def test_the_answer_is_read_from_yes_numbers_ranges_and_names(text: str, kind: str, hosts: set[str]) -> None:
    answer = parse_answer(text, SITES)
    assert answer is not None and answer.kind == kind and set(answer.hosts) == hosts


@pytest.mark.parametrize("text", ["", "найди ещё дома в Валенсии", "привет", "спасибо",
                                  "квартира 2 комнаты до 1000 евро", "дом 3 спальни в Мадриде"])
def test_a_text_that_is_no_answer_is_left_to_the_ordinary_handling(text: str) -> None:
    assert parse_answer(text, SITES) is None


def test_numbers_that_match_nothing_are_reported() -> None:
    answer = parse_answer("кроме 3 и 9", SITES)
    assert answer is not None and set(answer.hosts) == {"olx.ua"} and answer.unknown == ("9",)


def test_applying_an_answer_splits_the_pending_sites() -> None:
    assert apply_answer(Answer("all"), SITES) == ({s.host for s in SITES}, set())
    approved, rejected = apply_answer(Answer("reject", frozenset({"olx.ua"})), SITES)
    assert rejected == {"olx.ua"} and len(approved) == 4
    approved, rejected = apply_answer(Answer("only", frozenset({"idealista.com"})), SITES)
    assert approved == {"idealista.com"} and len(rejected) == 4


# --- the question --------------------------------------------------------------------------------------


def test_the_list_is_numbered_grouped_by_query_and_ends_with_the_question() -> None:
    [text] = question_messages(SITES, saved=2)
    assert "Уже одобрены вами раньше: 2" in text
    assert "По запросу «pisos Madrid»:\n1. idealista.com\n2. fotocasa.es" in text
    assert "По запросу «квартира Мадрид»:\n3. olx.ua" in text
    assert text.endswith(QUESTION)


def test_a_long_list_is_split_to_fit_telegram_and_the_question_closes_the_last_part() -> None:
    many = [Site(f"site-number-{n:03d}.example.com", number=n, query=f"query {n // 10}") for n in range(1, 301)]
    parts = question_messages(many)
    assert len(parts) > 1 and all(len(p) <= MAX_MESSAGE + len(QUESTION) + 2 for p in parts)
    assert parts[-1].endswith(QUESTION) and all(QUESTION not in p for p in parts[:-1])
    joined = "\n".join(parts)
    assert all(f"{n}. site-number-{n:03d}" in joined for n in (1, 150, 300))


def test_the_buttons_fit_telegrams_callback_limit() -> None:
    cid = "0f3b7c1e-1d2c-4b5a-9e8f-7a6b5c4d3e2f"
    assert all(len(data.encode()) <= 64 for _, data in buttons(cid))


# --- the web stage waits for the answer -----------------------------------------------------------------

URLS = {
    "pisos Madrid": ["https://www.idealista.com/inmueble/1/", "https://www.fotocasa.es/es/alquiler/vivienda/madrid/2/d"],
    "piso alquiler Madrid": ["https://www.milanuncios.com/alquiler-de-pisos-en-madrid/piso-3.htm", "https://www.idealista.com/inmueble/4/"],
}


async def setup(clock: Clock | None = None):
    clock = clock or Clock()
    campaigns = MemoryCampaignStore()
    plan = plan_campaign(GOAL, vertical="real_estate", location="Madrid")
    cid = await campaigns.create(plan, chat_id=OWNER, requested_by=OWNER, source_text=GOAL, actor="test")
    await campaigns.set_state(cid, "running", "test")
    sites = MemorySiteStore(now=clock)
    messenger = ButtonMessenger()
    gate = SiteGate(sites, messenger, config=GateConfig(remind_after_minutes=30), now=clock)
    web = MemoryWebStore(campaigns, now=clock)
    fetcher = FakeFetcher()
    worker = WebSearchWorker(campaigns, web, FakeSearcher(URLS), fetcher, ListGenerator(list(URLS)), gate=gate,
                             config=WebSearchConfig(queries_per_tick=1), now=clock)
    return campaigns, cid, sites, messenger, web, fetcher, worker, SiteDesk(sites)


async def ticks(worker: WebSearchWorker, n: int) -> None:
    for _ in range(n):
        await worker.tick()


async def test_no_page_is_read_before_the_person_approves_the_sites_of_all_queries() -> None:
    _, cid, _, messenger, web, fetcher, worker, desk = await setup()
    await ticks(worker, 6)
    assert fetcher.fetched == [], "nothing is read while the list waits for an answer"
    [(chat, text, keys)] = messenger.asks
    assert chat == OWNER and "1. idealista.com" in text and "2. fotocasa.es" in text and "3. milanuncios.com" in text
    assert [label for label, _ in keys] == ["✅ Все", "✏️ Убрать некоторые"]
    assert "жду подтверждения сайтов" in web.runs[cid].progress

    decision = await desk.on_text(OWNER, "кроме 3")
    assert decision is not None and decision.done
    assert "Убрал: 3. milanuncios.com" in decision.reply and "Ищу по 2 сайтам" in decision.reply
    await ticks(worker, 30)
    assert fetcher.fetched and not any("milanuncios.com" in u for u in fetcher.fetched)
    assert {u.split("/")[2] for u in fetcher.fetched} == {"www.idealista.com", "www.fotocasa.es"}
    assert [r.detail for r in web.urls[cid].values() if "milanuncios" in r.url] == ["site_rejected"]
    assert await desk.on_text(OWNER, "да") is None, "the answered list takes no more answers"


async def test_the_next_search_reads_approved_sites_at_once_never_offers_rejected_ones_and_searches_them_first() -> None:
    campaigns, _, sites, messenger, _, _, worker, desk = await setup()
    await ticks(worker, 6)
    await desk.on_text(OWNER, "убери milanuncios")
    plan = plan_campaign(GOAL, vertical="real_estate", location="Madrid")
    second = await campaigns.create(plan, chat_id=OWNER, requested_by=OWNER, source_text=GOAL, actor="test")
    await campaigns.set_state(second, "running", "test")
    asked = len(messenger.asks)
    fetcher = FakeFetcher()
    generator = ListGenerator(list(URLS))
    worker2 = WebSearchWorker(campaigns, MemoryWebStore(campaigns), FakeSearcher(URLS), fetcher, generator,
                              gate=worker.gate, config=WebSearchConfig(queries_per_tick=2))
    await ticks(worker2, 10)
    assert len(messenger.asks) == asked, "no new site, no question"
    assert fetcher.fetched and not any("milanuncios.com" in u for u in fetcher.fetched)
    preferred = await worker.gate.preferred(OWNER, "real_estate", None)
    assert set(preferred) == {"idealista.com", "fotocasa.es"}
    assert (await sites.saved(OWNER, "real_estate", ["milanuncios.com"])) == {"milanuncios.com": "rejected"}
    assert (await sites.saved(OWNER, "investors", ["milanuncios.com"])) == {}, "decisions are per mode"


async def test_the_buttons_approve_all_or_ask_which_to_remove() -> None:
    _, cid, _, _, _, fetcher, worker, desk = await setup()
    await ticks(worker, 6)
    edit = await desk.on_button(OWNER, "edit", cid)
    assert edit.reply == EDIT_HINT and not edit.done
    other = await desk.on_button(99, "all", cid)
    assert not other.done, "only the person who gave the task answers"
    everything = await desk.on_button(OWNER, "all", cid)
    assert everything.done and "Ищу по 3 сайтам" in everything.reply
    await ticks(worker, 30)
    assert any("milanuncios.com" in u for u in fetcher.fetched)


async def test_one_reminder_when_the_list_waits_too_long_and_a_failed_send_is_retried() -> None:
    clock = Clock()
    _, _, _, messenger, _, _, worker, _ = await setup(clock)
    messenger.ask_fail = 1
    await ticks(worker, 6)
    assert len(messenger.asks) == 1, "the list failed once and was sent again on the next check"
    clock.advance(31 * 60)
    await ticks(worker, 3)
    assert [t for _, _, t in messenger.sent].count(REMINDER) == 1


async def test_the_control_plane_routes_a_text_answer_and_leaves_other_texts_alone() -> None:
    _, _, _, _, _, _, worker, desk = await setup()
    await ticks(worker, 6)
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({OWNER}))
    control = ControlPlane(settings, MemoryControlPlaneStore(), None, lambda _: None, sites=desk)
    reply = await control.handle_text(IncomingMessage(chat_id=OWNER, user_id=OWNER, message_id=1, text="кроме 2"))
    assert reply is not None and reply.text.startswith("Убрал: 2. fotocasa.es")
    other = await control.handle_text(IncomingMessage(chat_id=OWNER, user_id=OWNER, message_id=2, text="кроме 2"))
    assert other is None or not other.text.startswith("Убрал"), "no open list: an ordinary message"


# --- PostgreSQL ----------------------------------------------------------------------------------------


@needs_db
async def test_postgres_sites_from_the_question_to_the_next_search(pool) -> None:
    from bot.campaign.store import PostgresCampaignStore

    campaigns = PostgresCampaignStore(pool)
    plan = plan_campaign(GOAL, vertical="real_estate", location="Madrid")
    cid = await campaigns.create(plan, chat_id=OWNER, requested_by=OWNER, source_text=GOAL, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    store, messenger = PostgresSiteStore(pool), ButtonMessenger()
    gate = SiteGate(store, messenger)
    web = PostgresWebStore(pool)
    fetcher = FakeFetcher()
    worker = WebSearchWorker(campaigns, web, FakeSearcher(URLS), fetcher, ListGenerator(list(URLS)), gate=gate,
                             config=WebSearchConfig(queries_per_tick=1))
    await ticks(worker, 6)
    assert fetcher.fetched == [] and len(messenger.asks) == 1
    assert set(await web.queued_hosts(cid)) == {"idealista.com", "fotocasa.es", "milanuncios.com"}
    desk = SiteDesk(store)
    assert await desk.waiting(OWNER)
    decision = await desk.on_text(OWNER, "только 1, 2")
    assert decision is not None and "Убрал: 3. milanuncios.com" in decision.reply
    await ticks(worker, 30)
    assert fetcher.fetched and not any("milanuncios.com" in u for u in fetcher.fetched)
    rows = {r["host"]: r["status"] for r in await pool.fetch("select host, status from search_sites")}
    assert rows == {"idealista.com": "approved", "fotocasa.es": "approved", "milanuncios.com": "rejected"}
    assert await store.approved_hosts(OWNER, "real_estate", "ES", 5) != []
    assert not await desk.waiting(OWNER)
