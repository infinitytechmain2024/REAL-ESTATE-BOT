"""Q69: the live status names and links only a listing portal (STATUS_HIDE_UNVETTED_SITES).

Before, «🔎 Сейчас ищу на сайте <a>host</a>» showed every site the web stage read: appraisers (arquitasa.com),
agencies, news, catalogues — to the person they looked like findings (sims b6-g7-s7, b6-g9-s7, b5-n2-s6).
"""

from __future__ import annotations

from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.status_text import READING, is_live_line, is_user_status, vetted_site_line
from bot.web_search.models import WebProgress
from bot.web_search.queries import listing_portal_host
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


async def test_flag_on_owners_keep_the_technical_lines() -> None:
    from tests.test_campaign_runner import create, make

    campaigns, _, messenger, _, _, runner, plan = make(status_hide_unvetted_sites=True)
    runner.web = Web(WebProgress("arquitasa.com", "http", 1, 0, url=APPRAISER), host="arquitasa.com")
    cid = await create(campaigns, plan)
    await runner.step(cid)
    owner = messenger.sent[-1][2].split("\n")
    assert owner[0].startswith("🎯 ") and owner[1] == READING and "сайты: страниц 37/60" in owner[-1]


def test_the_flag_is_off_by_default_and_reaches_the_runner(monkeypatch) -> None:
    monkeypatch.delenv("STATUS_HIDE_UNVETTED_SITES", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert CampaignRunnerSettings(_env_file=None).runner_config().status_hide_unvetted_sites is False
    monkeypatch.setenv("STATUS_HIDE_UNVETTED_SITES", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().status_hide_unvetted_sites is True
