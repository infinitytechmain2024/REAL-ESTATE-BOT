"""Q69: the live status names and links only a listing portal (STATUS_HIDE_UNVETTED_SITES).

Before, «🔎 Сейчас ищу на сайте <a>host</a>» showed every site the web stage read: appraisers (arquitasa.com),
agencies, news, catalogues — to the person they looked like findings (sims b6-g7-s7, b6-g9-s7, b5-n2-s6).
"""

from __future__ import annotations

from datetime import timedelta

from bot.campaign.final_report import FinalReporter, Tally
from bot.campaign.runs import OutcomeCount, SourceCount
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.status_text import READING, is_live_line, is_user_status, vetted_site_line
from bot.campaign.tolerance import Request
from bot.web_search.models import SiteReport, WebProgress
from bot.web_search.queries import listing_portal_host
from tests.test_final_report import Advice
from tests.test_user_status import GOAL, Web

APPRAISER = "https://www.arquitasa.com/?p=45022"
PORTAL = "https://www.idealista.com/inmueble/12345678/"


def test_vetted_site_line_links_a_portal_and_hides_any_other_site() -> None:
    assert vetted_site_line("idealista.com", PORTAL, listing_portal_host) == (
        f'🔎 Сейчас ищу на сайте <a href="{PORTAL}">idealista.com</a>')
    assert vetted_site_line("www.fotocasa.es", None, listing_portal_host) == (
        '🔎 Сейчас ищу на сайте <a href="https://fotocasa.es/">fotocasa.es</a>')
    for host, url in (("arquitasa.com", APPRAISER), (None, "https://elpais.com/economia/x.html"),
                      ("inmobiliaria-sol.es", None)):
        assert vetted_site_line(host, url, listing_portal_host) == READING
    assert vetted_site_line(None, None, listing_portal_host) is None
    assert vetted_site_line("<b>x</b>", "javascript:alert(1)", listing_portal_host) is None
    assert is_user_status(READING) and is_live_line(READING) and "<a" not in READING


async def _edits(flag: bool) -> list[str]:
    """Every text the person's status message had while the web stage read an appraiser, a portal, then news."""
    from tests.test_campaign_runner import make

    campaigns, _, messenger, _, clock, runner, plan = make(status_hide_unvetted_sites=flag)
    web = runner.web = Web(WebProgress("arquitasa.com", "http", 1, 0, url=APPRAISER), host="arquitasa.com")
    cid = await campaigns.create(plan, chat_id=-1, requested_by=8, source_text=GOAL, actor="telegram:8")
    await runner.step(cid)
    for host, url in (("arquitasa.com", "https://www.arquitasa.com/?p=2"), ("idealista.com", PORTAL),
                      ("elpais.com", "https://elpais.com/economia/x.html")):
        web.progress, web.host = WebProgress(host, "http", 2, 0, url=url), host
        await runner.step(cid)  # within web_status_seconds: throttled
        clock.advance(11)
        await runner.step(cid)
    return [t for c, _, t in messenger.sent if c == -1] + [t for c, _, t in messenger.edits if c == -1]


async def test_flag_on_no_edit_of_the_status_names_or_links_a_site_that_is_not_a_portal() -> None:
    texts = await _edits(True)
    assert texts == [READING, f'🔎 Сейчас ищу на сайте <a href="{PORTAL}">idealista.com</a>', READING]
    assert not any(word in t for t in texts for word in ("arquitasa", "elpais"))
    assert all(is_user_status(t) for t in texts)


async def test_flag_off_every_site_is_shown_as_before() -> None:
    texts = await _edits(False)
    assert texts[0] == f'🔎 Сейчас ищу на сайте <a href="{APPRAISER}">arquitasa.com</a>'
    assert any("elpais.com</a>" in t for t in texts) and any("idealista.com</a>" in t for t in texts)


async def test_flag_on_owners_keep_the_site_line_with_the_host() -> None:
    from tests.test_campaign_runner import create, make

    campaigns, _, messenger, _, _, runner, plan = make(status_hide_unvetted_sites=True)
    runner.web = Web(WebProgress("arquitasa.com", "http", 1, 0, url=APPRAISER), host="arquitasa.com")
    cid = await create(campaigns, plan)
    await runner.step(cid)
    owner = messenger.sent[-1][2].split("\n")
    assert owner[0].startswith("🎯 ") and "сайты: страниц 37/60" in owner[-1]
    assert owner[1] == f'🔎 Сейчас ищу на сайте <a href="{APPRAISER}">arquitasa.com</a>'


# --- the final report -------------------------------------------------------------------------------------------

REPORTS = [SiteReport("idealista.com", links=5, refused=5),  # a portal that refused every page
           SiteReport("arquitasa.com", links=2, refused=2, unverified=True),  # a captcha, no finding
           SiteReport("agencia-x.es", links=3, refused=3),  # a 403, no finding
           SiteReport("noticias.es", links=2, read=2),
           SiteReport("inmoblog.es", links=2, read=2)]
SOURCES = [SourceCount("website", "noticias.es", 1, 2, 0, 0, 0), SourceCount("website", "inmoblog.es", 1, 2, 1, 1, 0)]
UNVETTED = ("arquitasa", "agencia-x", "noticias")


async def _report(hide: bool) -> tuple[str, dict]:
    advice = Advice("Поднимите бюджет на 10 %: 3 варианта дороже.", "Добавьте 2 соседних района.")
    text = await FinalReporter(advice).build("Цель", Request(), [OutcomeCount("sent", "exact", None, 1)], [], SOURCES,
                                             REPORTS, (), hide_unvetted=hide)
    return text, advice.facts[-1]


async def test_flag_on_the_users_report_names_only_portals_and_sites_with_a_card() -> None:
    text, facts = await _report(True)
    assert not any(word in text for word in UNVETTED), text
    assert "inmoblog.es" in text and "Idealista" in text  # a card sent from it / a portal
    hosts = [s["site"] for s in facts["sites"]]
    assert "inmoblog.es" not in hosts and not any(w in h for h in hosts for w in UNVETTED)
    assert facts["unreadable_sites"] == ["idealista.com"]


def test_flag_on_the_proxy_advice_is_for_portals_only() -> None:
    from bot.campaign.final_report import facts_for_model, fallback_recommendations, site_stats

    stats = site_stats(SOURCES, REPORTS)
    portal_only = [stats["idealista.com"]]
    tips = fallback_recommendations(facts_for_model({}, Tally(), portal_only, portal_only))
    assert any(t.startswith("Idealista не читается") for t in tips)
    assert not any("agencia-x" in t for t in tips)


async def test_flag_off_the_report_is_as_before() -> None:
    text, facts = await _report(False)
    assert "arquitasa.com — проверку никто не прошёл" in text and "agencia-x" not in text
    assert "arquitasa.com" in facts["unreadable_sites"] and "noticias.es" in [s["site"] for s in facts["sites"]]


async def test_the_runner_hides_sites_in_the_report_for_users_only() -> None:
    from bot.campaign import MemoryCampaignStore, plan_campaign
    from bot.campaign.runner import CampaignRunner, RunnerConfig
    from bot.campaign.runs import MemoryRunStore
    from tests.test_campaign_runner import Clock, FakeMessenger

    class Reporter:
        def __init__(self) -> None:
            self.hide: list[bool] = []

        async def build(self, *args, hide_unvetted: bool = False, **kwargs) -> str:
            self.hide.append(hide_unvetted)
            return "📋 Отчёт по поиску"

    seen = {}
    for flag in (False, True):
        for who in (7, 8):
            campaigns, reporter, clock = MemoryCampaignStore(), Reporter(), Clock()
            store = MemoryRunStore(campaigns)
            runner = CampaignRunner(campaigns, store, FakeMessenger(), None, now=clock, owner_ids={7},
                                    final_report=reporter,
                                    config=RunnerConfig(relevance_fail_closed=False, status_hide_unvetted_sites=flag))
            cid = await campaigns.create(plan_campaign(GOAL), chat_id=-1, requested_by=who, source_text=GOAL, actor="t")
            await campaigns.set_state(cid, "running", "t")
            store.add_finding(cid, "f1", "🏠 f1", payload={}, original="post", vertical="real_estate", url="https://x.es/1")
            await campaigns.set_state(cid, "completed", "t")
            clock.at = (await campaigns.get(cid)).finished_at + timedelta(minutes=1)
            await runner.step(cid)
            seen[(flag, who)] = reporter.hide
    assert seen == {(False, 7): [False], (False, 8): [False], (True, 7): [False], (True, 8): [True]}


def test_the_flag_is_off_by_default_and_reaches_the_runner(monkeypatch) -> None:
    monkeypatch.delenv("STATUS_HIDE_UNVETTED_SITES", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert CampaignRunnerSettings(_env_file=None).runner_config().status_hide_unvetted_sites is False
    monkeypatch.setenv("STATUS_HIDE_UNVETTED_SITES", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().status_hide_unvetted_sites is True
