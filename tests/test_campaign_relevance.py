"""Only concrete offers that answer the task reach users: listing kind, country, area, the AI relevance check,
and search queries that always carry the campaign's place (the Moscow catalog card from the owner's bug)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bot.campaign import MemoryCampaignStore, geo, plan_campaign
from bot.campaign import offers as near
from bot.campaign.models import Campaign
from bot.campaign.relevance import (
    SCHEMA,
    OpenRouterRelevanceJudge,
    Relevance,
    RelevanceError,
    finding_data,
    parse_relevance,
    task_data,
    user_phrase,
)
from bot.campaign.runner import CampaignRunner, RunnerConfig, campaign_request
from bot.campaign.runs import MemoryRunStore
from bot.campaign.tolerance import Request, classify, min_area_of, request_for
from bot.social_search.queries import QueryContext, QueryPlanner, SocialQuery, normalise_query
from bot.social_search.queries import localise as social_localise
from bot.web_search.models import GeneratedQuery
from bot.web_search.queries import (
    FallbackQueryGenerator,
    QueryTask,
    TemplateQueryGenerator,
    localise,
)
from bot.web_search.searxng import SearchHit, SearxngClient
from bot.web_search.store import MemoryWebStore
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, query_task
from tests.test_near_match import ButtonMessenger

# The owner's task (Ukrainian voice), as intake turns it into the campaign's goal text (PR #33).
TASK = ("покупка Тип: участок. Задача: надай мені земельні ділянки від 2000 м² з будинками або без, в передмісті "
        "Мадрида, близькість до метро 5 хв на машині, ділянка має бути для забудови. Главное: земельный участок; "
        "покупка; пригород Мадрида; площадь от 2000 м²; под застройку. Дополнительно: с домом или без; "
        "до метро 5 минут на машине.")
CHAT, USER = 5151, 42
MOSCOW_CARD = {
    "schema_version": "analysis-v3",
    "summary": "Купить участок у метро в Москве",
    "summary_ru": ("Купить участок у метро в Москве — 43 объявления о продаже участков рядом с метро на МирКвартир… "
                   "Средняя стоимость объектов в Москве 28609258 руб."),
    "location": "Москва", "price_amount": 28_609_258, "price_currency": "RUB", "deal_type": "sale",
    "property_type": "land", "original_post_link": "https://dom.mirkvartir.ru/Москва/Участки-у-метро/",
}


def plot(area: float | None, *, kind: str | None = "offer", price: float = 450_000, **extra) -> dict:
    return {"schema_version": "analysis-v4", "summary": "Parcela urbanizable",
            "summary_ru": "Продаётся участок под застройку в Боадилья-дель-Монте, 5 минут на машине до метро.",
            "location": "Boadilla del Monte, Madrid", "country": "ES", "price_amount": price, "price_currency": "EUR",
            "deal_type": "sale", "property_type": "land", "area_m2": area, "listing_kind": kind,
            "original_post_link": "https://www.idealista.com/inmueble/12345/", **extra}


def madrid_request() -> Request:
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    return request_for(plan.constraints, location=plan.location, vertical=plan.vertical, text=TASK)


# --- deterministic guards ---------------------------------------------------------------------


def test_the_minimum_area_is_read_from_the_task() -> None:
    assert min_area_of(TASK) == 2000
    assert min_area_of("площадь от 1 500 м2") == 1500
    assert min_area_of("parcela desde 1.000 m²") == 1000
    assert min_area_of("від 20 соток") == 2000 and min_area_of("от 1 га") == 10_000
    assert min_area_of("до метро 5 минут, до 300 000 €") is None
    request = madrid_request()
    assert (request.min_area, request.deal, request.location, request.country_code) == (2000, "sale", "Madrid", "ES")


def test_the_moscow_catalog_card_is_excluded_by_the_rules_alone() -> None:
    request = madrid_request()
    assert classify(MOSCOW_CARD, request).bucket == "excluded"
    # Each signal on its own is enough.
    bare = {"summary_ru": "Участок у метро", "deal_type": "sale", "property_type": "land"}
    assert classify(bare, request).bucket == "similar"  # no area given, a minimum asked: unverified
    assert classify({**bare, "location": "Москва"}, request).bucket == "excluded"
    assert classify({**bare, "country": "RU"}, request).bucket == "excluded"
    assert classify({**bare, "price_amount": 28_609_258, "price_currency": "RUB"}, request).bucket == "excluded"
    assert classify({**bare, "original_post_link": "https://dom.mirkvartir.ru/x/"}, request).bucket == "excluded"
    assert classify({**bare, "listing_kind": "catalog"}, request).bucket == "excluded"
    assert classify({**bare, "summary_ru": "43 объявления о продаже участков"}, request).bucket == "excluded"
    assert classify({**bare, "summary_ru": "Средняя стоимость участков 300 000 €"}, request).bucket == "excluded"
    # A Spanish host, a generic one and Madrid itself never exclude.
    bare = {**bare, "area_m2": 2500}
    for link in ("https://www.idealista.com/inmueble/1/", "https://www.fotocasa.es/x", "https://x.eu/a",
                 "https://www.facebook.com/groups/1/posts/2/"):
        assert classify({**bare, "original_post_link": link}, request).bucket == "exact"
    assert classify({**bare, "location": "Мадрид, Испания", "country": "ES"}, request).bucket == "exact"


@pytest.mark.parametrize(("area", "bucket"), [
    (2500, "exact"), (2000, "exact"), (1900, "exact"), (1800, "exact"),  # ±10 % is the criterion itself
    (1600, "similar"), (1500, "similar"),  # 75-90 %: offered with «Одобрить»
    (1400, "excluded"), (900, "excluded"),
    (None, "similar"),  # an unknown area against a minimum is unverified (fail closed)
])
def test_madrid_plots_by_area(area: float | None, bucket: str) -> None:
    match = classify(plot(area), madrid_request())
    assert match.bucket == bucket
    if bucket == "similar":
        assert match.why == "area" and match.area == area


def test_a_catalog_page_or_a_wanted_post_in_madrid_is_excluded() -> None:
    request = madrid_request()
    assert classify(plot(2500, kind="catalog"), request).bucket == "excluded"
    assert classify(plot(2500, kind="wanted"), request).bucket == "excluded"
    assert classify(plot(2500, kind="other"), request).bucket == "excluded"
    old = {k: v for k, v in plot(2500).items() if k != "listing_kind"}  # analysis-v3: an offer
    assert classify(old, request).bucket == "exact"
    # Investors: a catalog is still not one lead, but someone seeking investment is.
    assert classify(plot(None, kind="catalog"), request, vertical="investors").bucket == "excluded"
    assert classify(plot(None, kind="wanted"), request, vertical="investors").bucket == "exact"


def test_geo_helpers() -> None:
    assert geo.foreign_tld("dom.mirkvartir.ru", "ES") and geo.foreign_tld("olx.ua", "ES")
    assert geo.foreign_tld("otodom.pl", "ES") and geo.foreign_tld("krisha.kz", "ES")
    assert not any(geo.foreign_tld(h, "ES") for h in ("idealista.com", "fotocasa.es", "x.net", "x.org", "x.eu",
                                                      "x.info", "x.io", "localhost", None))
    assert not geo.foreign_tld("dom.ria.com.ua", "UA") and geo.foreign_tld("cian.ru", "UA")
    assert geo.place_countries("Москва, у метро") == {"RU"}
    assert geo.place_countries("Pozuelo de Alarcón") == set()
    assert geo.place_countries("Kyiv") == {"UA"} and geo.place_countries("Madrid") == {"ES"}


# --- the AI relevance check -------------------------------------------------------------------


class FakeJudge:
    model = "fake/judge"

    def __init__(self, *verdicts: Relevance | Exception) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[tuple[dict, dict]] = []

    async def judge(self, task: dict, finding: dict) -> Relevance:
        self.calls.append((task, finding))
        verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        if isinstance(verdict, Exception):
            raise verdict
        return verdict


async def setup(judge: FakeJudge | None = None, **config):  # fail-closed off unless a test asks for it
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, config=RunnerConfig(**{"window_cooldown_seconds": 0, "relevance_fail_closed": False, **config}),
                            relevance=judge)
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=TASK, actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)  # the search is still running
    return campaigns, store, messenger, runner, cid


def add(store: MemoryRunStore, cid: str, fid: str, payload: dict) -> None:
    store.add_finding(cid, fid, f"🏠 {fid}", payload=payload, original=f"post {fid}", vertical="real_estate")


REJECT = Relevance("reject", "Каталог объявлений, а не один участок.")
MATCH = Relevance("match", "Участок под застройку в пригороде Мадрида.")


async def test_moscow_card_is_never_sent_with_the_ai_off_or_on() -> None:
    _, store, messenger, runner, cid = await setup(None)
    add(store, cid, "moscow", MOSCOW_CARD)
    await runner.tick()
    assert messenger.findings() == [] and messenger.asks == [] and store.buckets["moscow"][0] == "excluded"

    judge = FakeJudge(REJECT)
    _, store, messenger, runner, cid = await setup(judge)
    add(store, cid, "moscow", MOSCOW_CARD)
    # Without the rules' signals (no country, currency or .ru link) only the model can tell.
    add(store, cid, "anon", {"summary_ru": "Купить участок у метро — объявления о продаже участков",
                             "deal_type": "sale", "property_type": "land",
                             "original_post_link": "https://example.com/uchastki-u-metro/"})
    await runner.tick()
    assert messenger.findings() == [] and messenger.asks == []
    assert store.buckets["moscow"][0] == "excluded" and store.buckets["anon"][0] == "excluded"
    assert len(judge.calls) == 1  # the rules already excluded the Moscow card: no call for it
    task, finding = judge.calls[0]
    assert "от 2000 м²" in task["task"] and task["place"] == "Madrid" and task["min_area_m2"] == 2000
    assert task["country"] == "ES" and "comunidad de madrid" in task["place_region"]
    assert finding["link_host"] == "example.com" and finding["listing_kind"] == "offer"
    assert (await store.relevance(cid, "anon")).verdict == "reject"


async def test_madrid_plots_end_to_end_and_the_area_question() -> None:
    judge = FakeJudge(MATCH)
    _, store, messenger, runner, cid = await setup(judge)
    for fid, area in (("p2500", 2500), ("p1900", 1900), ("p1600", 1600), ("p1400", 1400)):
        add(store, cid, fid, plot(area))
    add(store, cid, "catalog", plot(2500, kind="catalog"))
    add(store, cid, "wanted", plot(2500, kind="wanted"))
    await runner.tick()
    assert {f: b for f, (b, _) in store.buckets.items()} == {
        "p2500": "exact", "p1900": "exact", "p1600": "similar", "p1400": "excluded", "catalog": "excluded",
        "wanted": "excluded"}
    assert len(messenger.findings()) == 2 and messenger.asks == []  # exact ones found: no question yet
    assert len(judge.calls) == 3  # excluded ones never go to the model


async def test_area_question_says_what_is_outside_the_criteria() -> None:
    _, store, messenger, runner, cid = await setup(None)
    add(store, cid, "p1600", plot(1600))
    await runner.tick()
    assert messenger.findings() == []
    assert [text for _, text, _ in messenger.asks] == [
        "По вашим критериям пока ничего не нашёл, но есть участки меньшей площади "
        "(~1 600 м² при запросе от 2 000 м²). Показать?"]


async def test_price_question_says_what_is_outside_the_criteria() -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, config=RunnerConfig(window_cooldown_seconds=0))
    goal = "Купить участок в Мадриде до 50 000 €"
    cid = await campaigns.create(plan_campaign(goal, vertical="real_estate"), chat_id=CHAT, requested_by=USER, source_text=goal, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    add(store, cid, "p60", plot(None, price=60_000))
    await runner.tick()
    assert [text for _, text, _ in messenger.asks] == [
        "По вашим критериям пока ничего не нашёл, но есть участки чуть дороже "
        "(например ~60 000 € при запросе ~50 000 €). Показать?"]


async def test_near_verdict_is_offered_with_the_ai_phrase_and_never_sent_before_approval() -> None:
    judge = FakeJudge(Relevance("near", "Метро в 15 минутах.", "дальше от метро"))
    _, store, messenger, runner, cid = await setup(judge)
    add(store, cid, "far", plot(2500))
    await runner.tick()
    assert store.buckets["far"][0] == "similar" and messenger.findings() == []
    assert [text for _, text, _ in messenger.asks] == [
        "По вашим критериям пока ничего не нашёл, но есть участки дальше от метро. Показать?"]


async def test_verdict_is_stored_once_and_a_failed_send_does_not_ask_again() -> None:
    judge = FakeJudge(MATCH)
    _, store, messenger, runner, cid = await setup(judge)
    add(store, cid, "p2500", plot(2500))
    messenger.fail = 1  # the first card cannot be sent: the finding goes back to the queue
    await runner.tick()
    assert messenger.findings() == [] and "p2500" not in store.buckets
    await runner.tick()
    await runner.tick()
    assert len(messenger.findings()) == 1 and len(judge.calls) == 1
    assert await store.relevance_calls(cid) == 1


async def test_ai_failure_falls_back_to_the_rules_and_pauses_the_model() -> None:
    judge = FakeJudge(RelevanceError("timeout"))
    _, store, messenger, runner, cid = await setup(judge)
    clock = [datetime(2026, 9, 27, 12, 0, tzinfo=UTC)]
    runner.now = lambda: clock[0]
    add(store, cid, "a", plot(2500))
    add(store, cid, "b", plot(1600))
    add(store, cid, "c", plot(2200))
    await runner.tick()
    # One failed call (stored, counted); the model is left alone for a minute, the rules decide meanwhile.
    assert len(judge.calls) == 1 and await store.relevance_calls(cid) == 1
    assert (await store.relevance(cid, "a")).verdict is None
    assert {f: b for f, (b, _) in store.buckets.items()} == {"a": "exact", "b": "similar", "c": "exact"}
    assert len(messenger.findings()) == 2
    clock[0] += timedelta(seconds=61)
    add(store, cid, "d", plot(2100))
    await runner.tick()
    assert len(judge.calls) == 2 and store.buckets["d"][0] == "exact"


async def test_the_relevance_cap_is_respected() -> None:
    judge = FakeJudge(REJECT)
    _, store, messenger, runner, cid = await setup(judge, max_relevance_calls=2)
    for fid in "abcd":
        add(store, cid, fid, plot(2500))
    await runner.tick()
    assert len(judge.calls) == 2 and await store.relevance_calls(cid) == 2
    # The first two were rejected by the model; past the cap the rules alone let the others through.
    assert {f: b for f, (b, _) in store.buckets.items()} == {"a": "excluded", "b": "excluded", "c": "exact",
                                                             "d": "exact"}
    assert len(messenger.findings()) == 2


async def judged(runner: CampaignRunner, campaign_id: str, store: MemoryRunStore, fid: str):
    (finding,) = [f for f in store.findings[campaign_id] if f.id == fid]
    return await runner._judge(await runner.campaigns.get(campaign_id), campaign_request(
        await runner.campaigns.get(campaign_id)), finding)


async def test_fail_closed_without_a_judge_holds_exact_as_unverified() -> None:
    _, store, messenger, runner, cid = await setup(None, relevance_fail_closed=True)
    add(store, cid, "a", plot(2500))
    await runner.tick()
    assert store.buckets["a"][0] == "similar" and messenger.findings() == []
    match = await judged(runner, cid, store, "a")
    assert (match.bucket, match.why, match.note) == ("similar", "unverified", "Не проверено ИИ: сбой проверки")


async def test_fail_closed_after_a_judge_failure_holds_exact_as_unverified() -> None:
    _, store, messenger, runner, cid = await setup(FakeJudge(RelevanceError("timeout")), relevance_fail_closed=True)
    add(store, cid, "a", plot(2500))
    await runner.tick()
    assert store.buckets["a"][0] == "similar" and messenger.findings() == []
    match = await judged(runner, cid, store, "a")
    assert (match.bucket, match.note) == ("similar", "Не проверено ИИ: сбой проверки")


async def test_fail_closed_past_the_cap_holds_exact_as_unverified() -> None:
    judge = FakeJudge(MATCH)
    _, store, messenger, runner, cid = await setup(judge, max_relevance_calls=1, relevance_fail_closed=True)
    add(store, cid, "a", plot(2500))
    add(store, cid, "b", plot(2600))
    await runner.tick()
    assert store.buckets["b"][0] == "similar" and len(messenger.findings()) == 1
    match = await judged(runner, cid, store, "b")
    assert (match.bucket, match.why, match.note) == ("similar", "unverified", "Не проверено ИИ: лимит проверок исчерпан")


async def test_fail_closed_never_touches_investors_and_can_be_switched_off() -> None:
    _, store, messenger, runner, cid = await setup(None, relevance_fail_closed=True)
    store.add_finding(cid, "inv", "🏠 inv", payload=plot(None), original="post", vertical="investors")
    await runner.tick()
    assert store.buckets["inv"][0] == "exact"
    _, store, messenger, runner, cid = await setup(None, relevance_fail_closed=False)
    add(store, cid, "a", plot(2500))
    await runner.tick()
    assert store.buckets["a"][0] == "exact" and len(messenger.findings()) == 1


def test_parse_relevance_accepts_drift_and_keeps_users_away_from_english() -> None:
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])
    ok = parse_relevance('{"verdict": "near", "reason": "Метро дальше.", "deviation_ru": "Дальше от метро"}')
    assert (ok.verdict, ok.reason, ok.deviation) == ("near", "Метро дальше.", "дальше от метро")
    fenced = parse_relevance('```json\n{"verdict": "Rejected", "reason": "Moscow"}\n```')
    assert (fenced.verdict, fenced.deviation) == ("reject", None)
    assert parse_relevance('{"decision": "match", "why": "ok", "extra": 1}').verdict == "match"
    assert parse_relevance('{"verdict": "no"}').verdict == "reject"
    english = parse_relevance('{"verdict": "near", "reason": "x", "deviation_ru": "farther from the metro"}')
    assert english.deviation is None
    assert parse_relevance('{"verdict": "match", "deviation_ru": "дальше от метро"}').deviation is None
    with pytest.raises(ValueError):
        parse_relevance('{"verdict": "maybe"}')
    assert user_phrase("без лицензии на строительство.") == "без лицензии на строительство"
    assert user_phrase("m² 2000") is None and user_phrase("") is None


async def test_openrouter_judge_structured_then_json_object_and_timeout() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "no structured outputs"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"verdict": "reject", "reason": "Москва, а не Мадрид.", "deviation_ru": ""}, ensure_ascii=False)}}]})

    judge = OpenRouterRelevanceJudge(api_key="k", model="openai/gpt-4o-mini",
                                     client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    campaign = Campaign("c1", plan, "running", 1, 1, TASK, None, None, datetime.now(UTC))
    verdict = await judge.judge(task_data(campaign), finding_data(MOSCOW_CARD))
    assert (verdict.verdict, verdict.reason, verdict.model) == ("reject", "Москва, а не Мадрид.", "openai/gpt-4o-mini")
    assert [b["response_format"]["type"] for b in seen] == ["json_schema", "json_object"]
    assert seen[0]["response_format"]["json_schema"]["strict"] is True
    data = json.loads(seen[0]["messages"][1]["content"].split("\n", 1)[1])
    assert data["finding"]["link_host"] == "dom.mirkvartir.ru" and data["finding"]["currency"] == "RUB"
    assert "±10 %" in seen[0]["messages"][0]["content"]

    failing = OpenRouterRelevanceJudge(api_key="k", client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(RelevanceError, match="http_error"):
        await failing.judge({}, {})
    with pytest.raises(ValueError):
        OpenRouterRelevanceJudge(api_key="")


# --- search queries always carry the place ------------------------------------------------------


def madrid_task() -> QueryTask:
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    return QueryTask(plan.goal, TASK, plan.location, dict(plan.location_aliases), plan.vertical,
                     dict(plan.constraints), tuple(plan.languages))


MADRID_WORDS = ("madrid", "мадрид")


def names_madrid(text: str) -> bool:
    return any(word in text.casefold() for word in MADRID_WORDS)


class BareModel:
    """A model that forgets the place, the owner's bug."""

    model = "fake"

    async def generate(self, task, *, used, count):
        return [GeneratedQuery("купить участок у метро", "ru"), GeneratedQuery("участок под застройку", "ru"),
                GeneratedQuery("ділянка під забудову", "uk"), GeneratedQuery("terreno urbanizable", "es"),
                GeneratedQuery("building plot near metro", "en"), GeneratedQuery("parcela Las Rozas", "es"),
                GeneratedQuery("terreno Comunidad de Madrid", "es")]


async def test_every_web_query_for_madrid_names_the_place() -> None:
    task = madrid_task()
    assert task.spanish and task.search_language == "es-ES"
    round_ = await FallbackQueryGenerator(BareModel()).generate(task, used=[], count=12)
    assert len(round_) == 12 and all(names_madrid(q.text) for q in round_)
    # Russian/Ukrainian queries carry the Spanish name in Latin letters and stay a minority.
    cyrillic = [q for q in round_ if q.language in ("ru", "uk")]
    assert cyrillic and all("Madrid" in q.text for q in cyrillic) and len(cyrillic) <= 3 + 1
    assert "terreno Comunidad de Madrid Madrid España" in [q.text for q in round_]
    for _ in range(3):
        template = await TemplateQueryGenerator().generate(task, used=[], count=12)
        assert all(names_madrid(q.text) for q in template)
    assert localise([GeneratedQuery("x" * 118, "es")], task) == []  # no room for the place: dropped


class RecordingSearcher:
    def __init__(self, hits: list[str]) -> None:
        self.hits, self.calls = hits, []

    async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]:
        self.calls.append((query, language))
        return [SearchHit(u) for u in self.hits]


class OneRound:
    model = "fake"

    async def generate(self, task, *, used, count):
        return [] if used else [GeneratedQuery("купить участок у метро", "ru")]


async def test_web_worker_searches_spain_and_drops_foreign_sites() -> None:
    campaigns = MemoryCampaignStore()
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text=TASK, actor="test")
    await campaigns.set_state(cid, "running", "test")
    store = MemoryWebStore(campaigns)
    searcher = RecordingSearcher(["https://dom.mirkvartir.ru/Москва/Участки-у-метро/", "https://www.olx.ua/a/1",
                                  "https://www.idealista.com/inmueble/12345/", "https://www.fotocasa.es/es/x/1/d"])
    worker = WebSearchWorker(campaigns, store, searcher, None, OneRound(),  # type: ignore[arg-type]
                             config=WebSearchConfig(cover_portals=False))
    await worker.tick()  # the round
    await worker.tick()  # the search
    assert searcher.calls == [("купить участок у метро Madrid Испания", "es-ES")]
    queued = {u.url for u in await store.next_urls(cid, 10)}
    assert queued == {"https://www.idealista.com/inmueble/12345/", "https://www.fotocasa.es/es/x/1/d"}
    assert query_task(await campaigns.get(cid)).country == "ES"


async def test_searxng_receives_es_es() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["language"] == "es-ES"
        return httpx.Response(200, json={"results": [{"url": "https://www.idealista.com/inmueble/1/"}]})

    client = SearxngClient("http://searxng:8080", client=httpx.AsyncClient(
        base_url="http://searxng:8080", transport=httpx.MockTransport(handler)))
    assert [h.url for h in await client.search("terreno Madrid", language=madrid_task().search_language)] == [
        "https://www.idealista.com/inmueble/1/"]


def social_context() -> QueryContext:
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    return QueryContext(task=TASK, goal=plan.goal, location=plan.location, vertical=plan.vertical,
                        location_aliases=dict(plan.location_aliases),
                        constraints={k: v for k, v in plan.constraints.items() if v is not None},
                        seeds={k: list(v) for k, v in plan.query_seeds.items()})


async def test_social_queries_name_the_place() -> None:
    context = social_context()
    raw = [normalise_query("instagram", "hashtag", "terrenoenventa", "es"),
           normalise_query("instagram", "hashtag", "участок", "ru"),
           normalise_query("instagram", "keyword", "участок у метро", "ru"),
           normalise_query("instagram", "keyword", "parcela Madrid", "es")]
    fixed = social_localise([q for q in raw if q is not None], context)
    assert [q.text for q in fixed] == ["terrenoenventamadrid", "parcela Madrid", "участок у метро Madrid"]

    class Model:
        async def generate(self, context, platform, used, count):
            return [SocialQuery("tiktok", "keyword", "участок у метро", "участок у метро", "ru")]

    for generator in (None, Model()):
        for platform in ("tiktok", "instagram", "linkedin"):
            queries = await QueryPlanner(generator).round(context, platform, set(), [], 6)
            assert queries and all(names_madrid(q.text) for q in queries), (platform, queries)
            assert all("Madrid" in q.text or "madrid" in q.text for q in queries if q.language in ("ru", "uk"))


def test_campaign_request_reads_the_area_from_the_task() -> None:
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    campaign = Campaign("c1", plan, "running", 1, 1, TASK, None, None, datetime.now(UTC))
    assert campaign_request(campaign).min_area == 2000


def test_offer_question_texts() -> None:
    assert near.similar_question(None, exact_found=False) == (
        "По вашим критериям пока ничего не нашёл, но есть похожие варианты. Показать?")
    assert near.similar_question(near.Deviation("price", "~59 000 €", "~50 000 €"), exact_found=True) == (
        "Есть ещё похожие варианты (например ~59 000 € при запросе ~50 000 €). Показать?")
    assert near.similar_question(near.Deviation("other", phrase="дальше от метро"), exact_found=True) == (
        "Есть ещё похожие варианты (дальше от метро). Показать?")


# --- PostgreSQL (migration 023) -----------------------------------------------------------------

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


async def _seed(pool, cid: str, payloads: dict[str, dict]) -> dict[str, str]:
    ids: dict[str, str] = {}
    async with pool.acquire() as conn:
        source = await conn.fetchval(
            """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, acquisition_method, state)
               values ('facebook', 'group', 'real_estate', 'https://www.facebook.com/groups/parcelas/',
                       'facebook_connector', 'active') returning id""")
        batch = await conn.fetchval(
            """insert into acquisition_batches (platform, acquisition_method, vertical, state, max_items, campaign_id)
               values ('facebook', 'facebook_connector', 'real_estate', 'succeeded', 1, $1::uuid) returning id""", cid)
        item = await conn.fetchval(
            """insert into acquisition_batch_items (batch_id, source_id, sequence_no, state)
               values ($1, $2, 1, 'succeeded') returning id""", batch, source)
        run = await conn.fetchval(
            """insert into acquisition_runs (source_id, batch_item_id, state, acquisition_method)
               values ($1, $2, 'succeeded', 'facebook_connector') returning id""", source, item)
        for n, (name, payload) in enumerate(payloads.items()):
            post = await conn.fetchval(
                """insert into collected_posts (acquisition_run_id, source_id, platform_post_id, canonical_url,
                                                body_text, state, content_hash)
                   values ($1, $2, $3, $4, 'Vendo parcela', 'analysed', $5) returning id""",
                run, source, str(n), f"https://www.facebook.com/groups/parcelas/posts/{n}/", uuid.uuid4().hex)
            ids[name] = str(await conn.fetchval(
                """insert into findings (post_id, source_id, vertical, finding_type, state, structured_payload,
                                         confidence, dedupe_key)
                   values ($1, $2, 'real_estate', 'real_estate_proposition', 'ready', $3::jsonb, 0.9, $4)
                   returning id""", post, source, json.dumps(payload, ensure_ascii=False), uuid.uuid4().hex))
    return ids


@needs_db
async def test_postgres_relevance_is_stored_once_and_excluded_findings_are_never_sent(pool) -> None:
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.store import PostgresCampaignStore
    from bot.orchestra.store import SafetyLimits

    campaigns = PostgresCampaignStore(pool)
    store = PostgresRunStore(pool, SafetyLimits())
    messenger = ButtonMessenger()
    judge = FakeJudge(REJECT, MATCH)
    runner = CampaignRunner(campaigns, store, messenger, None, relevance=judge)
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=TASK, actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    ids = await _seed(pool, cid, {"moscow": MOSCOW_CARD, "anon": plot(None, country=None, location=None),
                                  "good": plot(2500)})
    await runner.step(cid)
    await runner.step(cid)
    rows = dict(await pool.fetch("select finding_id::text, bucket || ':' || state from campaign_findings"))
    assert rows == {ids["moscow"]: "excluded:held", ids["anon"]: "excluded:held", ids["good"]: "exact:sent"}
    assert len(messenger.findings()) == 1 and "Москва" not in messenger.findings()[0]
    stored = dict(await pool.fetch("select finding_id::text, verdict from campaign_finding_relevance"))
    assert stored == {ids["anon"]: "reject", ids["good"]: "match"} and len(judge.calls) == 2
    assert await store.relevance_calls(cid) == 2
    await store.save_relevance(cid, ids["good"], Relevance("reject", "x"))  # never overwritten
    assert (await store.relevance(cid, ids["good"])).verdict == "match"
    assert await store.relevance(cid, ids["moscow"]) is None
