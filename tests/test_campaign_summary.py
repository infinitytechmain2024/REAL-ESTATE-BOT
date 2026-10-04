"""The end-of-campaign summary «Итог поиска»: what each source gave, sent once."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.relevance import finding_data
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore, SourceCount, StreamFinding
from bot.campaign.summary import plural, summary_text
from bot.web_search.models import SEARCH_RESULT_NOTE, SiteReport
from bot.web_search.urls import SPAIN_PORTALS
from tests.test_campaign_runner import SUMMARY, Clock, FakeDiscovery, FakeMessenger

PORTALS = SPAIN_PORTALS
OWNER, USER = 1, 2


def test_plural() -> None:
    assert [plural(n, "ссылка", "ссылки", "ссылок") for n in (1, 2, 5, 11, 21, 22, 112)] == [
        "1 ссылка", "2 ссылки", "5 ссылок", "11 ссылок", "21 ссылка", "22 ссылки", "112 ссылок"]


def test_every_source_gets_its_line_with_what_it_gave() -> None:
    sources = [
        SourceCount("facebook", "facebook", sources=12, posts=340, relevant=9, sent=5, held=2),
        SourceCount("instagram", "instagram", sources=1, posts=20),
        SourceCount("website", "idealista.com", sources=1, posts=6, relevant=3, sent=2),
        SourceCount("website", "fotocasa.es", sources=1, posts=7, relevant=1, sent=1),
        SourceCount("website", "inmobiliaria-garcia.es", sources=1, posts=3, relevant=1, sent=1),
    ]
    reports = [
        SiteReport("idealista.com", queries=3, results=25, links=14, from_search=6, refused=8),
        SiteReport("fotocasa.es", queries=2, results=12, links=9, read=7, refused=2),
        SiteReport("inmobiliaria-garcia.es", links=3, read=3),
        SiteReport("yaencontre.com", queries=1, results=0),
        SiteReport("pisos.com", queries=1, results=4),
    ]
    text = summary_text("Участок под Мадридом", sources, reports, portals=PORTALS)
    lines = text.splitlines()
    assert lines[:3] == ["📊 Итог поиска", "🎯 Участок под Мадридом",
                         "Отправлено объявлений: 9 · ещё похожих вариантов: 2"]
    assert "Facebook — 12 групп, 340 постов → 5 объявлений (похожих: 2)" in lines
    assert "Instagram — 20 постов → 0 объявлений" in lines
    assert ("Idealista — 14 ссылок в поиске · по описанию из поиска 6 (сайт не пускает ботов) → 2 объявления"
            in lines)
    assert "Fotocasa — 9 ссылок в поиске · прочитано 7 → 1 объявление" in lines
    assert "inmobiliaria-garcia.es — 3 ссылки в поиске · прочитано 3 → 1 объявление" in lines
    assert lines.index("Idealista — 14 ссылок в поиске · по описанию из поиска 6 (сайт не пускает ботов) → 2 объявления") \
        < lines.index("Fotocasa — 9 ссылок в поиске · прочитано 7 → 1 объявление")  # portals in priority order
    nothing = lines[-1]
    assert nothing.startswith("Не нашлось в поиске: Yaencontre, Pisos.com, Habitaclia")


def test_idealista_and_fotocasa_always_say_why_they_gave_nothing() -> None:
    reports = [SiteReport("idealista.com", queries=3, results=0), SiteReport("fotocasa.es", queries=2, results=5),
               SiteReport("agencia.es", links=2, read=2)]
    lines = summary_text("Квартира", [], reports, portals=PORTALS).splitlines()
    assert "Idealista — поиск ничего не вернул (3 запроса)" in lines
    assert "Fotocasa — в поиске были только чужие или уже прочитанные ссылки" in lines
    refused = summary_text("Квартира", [], [SiteReport("idealista.com", queries=1, results=3, links=3, refused=3)],
                           portals=PORTALS).splitlines()
    assert "Idealista — 3 ссылки в поиске · сайт не дал прочитать страницы → 0 объявлений" in refused


def test_many_small_sites_are_summed_and_without_a_web_stage_no_portals_are_listed() -> None:
    reports = [SiteReport(f"site{n}.es", links=1, read=1) for n in range(12)]
    lines = summary_text("Квартира", [SourceCount("website", "site3.es", posts=1, sent=1)], reports,
                         portals=PORTALS).splitlines()
    assert lines[lines.index("site3.es — 1 ссылка в поиске · прочитано 1 → 1 объявление") + 1].startswith("site")
    assert "Другие сайты (4) — прочитано 4 → 0 объявлений" in lines
    only_facebook = summary_text("Квартира", [SourceCount("facebook", "facebook", 3, 40, 2, 1)], [], portals=())
    assert only_facebook.splitlines()[4:] == ["Facebook — 3 группы, 40 постов → 1 объявление"]
    assert summary_text("Квартира", [], [], portals=()).endswith("Источники ничего не дали.")


def test_the_relevance_check_sees_the_listing_and_knows_a_search_result() -> None:
    original = "Terreno en venta en Boadilla - idealista\nTerreno de 1.200 m². 480.000 €.\n\nСсылка: x\n" \
               + SEARCH_RESULT_NOTE
    data = finding_data({"summary_ru": "Участок 1200 м²"}, original=original)
    assert data["excerpt"].startswith("Terreno en venta en Boadilla") and data["from_search"] is True
    assert finding_data({"summary_ru": "x"}, original="Piso en Madrid " * 200)["from_search"] is False
    assert len(finding_data({}, original="a " * 2000)["excerpt"]) == 700


# --- the runner sends it once -----------------------------------------------------------------


def build(messenger: FakeMessenger | None = None):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = messenger or FakeMessenger()
    clock = Clock()
    runner = CampaignRunner(campaigns, store, messenger, FakeDiscovery(campaigns, store, 3), now=clock,
                            config=RunnerConfig(), owner_ids={OWNER})
    return campaigns, store, messenger, clock, runner


async def finished(campaigns, store, state: str = "completed", requested_by: int = 1) -> str:
    cid = await campaigns.create(plan_campaign("квартира в Мадриде до 1200 евро"), chat_id=-100,
                                 requested_by=requested_by,
                                 source_text="квартира в Мадриде до 1200 евро", actor="test")
    store.findings[cid] = [StreamFinding("f1", "card", None, url="https://www.facebook.com/groups/1/posts/2")]
    store.posts_read[cid] = {("facebook", "facebook"): (4, 120)}
    await campaigns.set_state(cid, "running", "test")
    await campaigns.set_state(cid, state, "test")
    return cid


async def test_a_finished_campaign_gets_one_summary_above_its_final_status() -> None:
    campaigns, store, messenger, clock, runner = build()
    cid = await finished(campaigns, store)
    clock.at = (await campaigns.get(cid)).finished_at + timedelta(minutes=1)
    await runner.step(cid)
    await runner.step(cid)
    [summary] = messenger.summaries()
    # The payload-less finding is held as «похожее» (the rules cannot place it): it counts as waiting.
    assert "Facebook — 4 группы, 120 постов → 0 объявлений (похожих: 1)" in summary.splitlines()
    assert messenger.sent[-1][2] == messenger.statuses()[-1]  # the status message stays the last one


async def test_old_failed_or_unsent_summaries() -> None:
    campaigns, store, messenger, clock, runner = build()
    old = await finished(campaigns, store)
    clock.at = (await campaigns.get(old)).finished_at + timedelta(hours=3)  # ended before this existed
    await runner.step(old)
    failed = await finished(campaigns, store, state="failed")
    await runner.step(failed)
    user = await finished(campaigns, store, requested_by=USER)  # a normal user: no technical report
    clock.at = (await campaigns.get(user)).finished_at
    await runner.step(user)
    assert messenger.summaries() == []

    campaigns, store, messenger, clock, runner = build()
    cid = await finished(campaigns, store, state="cancelled")
    clock.at = (await campaigns.get(cid)).finished_at
    messenger.fail = 1  # Telegram down: the claim is given back, the next tick sends it
    await runner.step(cid)
    await runner.step(cid)
    assert len(messenger.summaries()) == 1 and messenger.summaries()[0].startswith(SUMMARY)


def test_summary_states_are_reported_as_a_dataclass_copy() -> None:
    # SiteReport/SourceCount are plain frozen values the summary reads by name.
    assert replace(SiteReport("a.es"), links=2).links == 2
