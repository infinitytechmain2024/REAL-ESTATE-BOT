"""Normal users see only short, fixed statuses; owners keep the technical lines."""

from __future__ import annotations

import re

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.models import CampaignLimits
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.status_text import (
    CHECKING,
    DONE,
    FACEBOOK,
    NOTHING,
    SEARCHING,
    campaign_label,
    is_user_status,
    site_line,
    social_line,
    user_status,
)
from bot.operators import OperatorSet
from bot.orchestra.dispatcher import OrchestraDispatcher
from tests.test_campaign_runner import GOAL, Clock, FakeDiscovery, FakeMessenger
from tests.test_orchestra_dispatcher import FakeStore, claimed

USER, OWNER, CHAT = 7, 99, -100
ALLOWED = {"Принято. Начинаю поиск.", "Ищу…", "Ищу в Facebook…", "🔎 Поиск завершён. Проверяю найденное…",
           "Поиск завершён.", "Пока ничего подходящего не нашёл.",
           "Ищу в TikTok…", "Ищу в Instagram…", "Ищу в LinkedIn…", "✅ Готово: отправлено 1"}
# the group being read, by its name, with its link embedded
GROUP_4 = '🔎 Сейчас ищу в Facebook в группе <a href="https://www.facebook.com/groups/g004/">Group 4</a>'
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", re.I)
FORBIDDEN = ("window", "окно", "20", "batch", "campaign", "Кампания", "кампания", "orchestra", "Orchestra",
             "/campaign", "групп", "Queue", "Пауза", "verification", "🎯")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def assert_user_safe(text: str) -> None:
    if text.startswith("🔎 Сейчас ищу "):  # one place, linked; checked by is_user_status
        assert is_user_status(text), text
        return
    from bot.campaign.status_text import LIMIT_NOTE

    text = text.replace(f"\n{LIMIT_NOTE}", "")  # the honest Facebook-limit note is written for users
    assert not UUID.search(text), text
    for word in FORBIDDEN:
        assert word not in text, (word, text)
    assert [w for w in LATIN_WORD.findall(text) if w not in {"Facebook", "TikTok", "Instagram", "LinkedIn"}] == [], text


class FlakyMessenger(FakeMessenger):
    """Every edit fails with an arbitrary Telegram error."""

    async def edit(self, chat_id: int, message_id: int, text: str, *, parse_mode: str | None = None) -> None:
        raise RuntimeError("telegram 400: Bad Request: message is not modified")


def build(*, owners: frozenset[int] = frozenset({OWNER}), groups: int = 20, messenger: FakeMessenger | None = None):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = messenger or FakeMessenger()
    discovery = FakeDiscovery(campaigns, store, groups)
    clock = Clock()
    runner = CampaignRunner(campaigns, store, messenger, discovery, now=clock, owner_ids=owners,
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=120, analysis_grace_seconds=600))
    plan = plan_campaign(GOAL).model_copy(update={"limits": CampaignLimits(max_groups=groups)})
    return campaigns, store, messenger, clock, runner, plan


def statuses(messenger: FakeMessenger) -> list[str]:
    """Every status text sent or edited in (findings excluded), in the order shown."""
    return list(messenger.timeline)


def timeline(messenger: FakeMessenger) -> list[str]:
    shown: list[str] = []
    for text in statuses(messenger):
        if not shown or shown[-1] != text:
            shown.append(text)
    return shown


async def full_search(runner: CampaignRunner, campaigns, store, clock, plan, requested_by: int) -> str:
    """Discovery, a Facebook window read group by group, a verification pause, analysis, a finding, done."""
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=requested_by, source_text=GOAL,
                                 actor=f"telegram:{requested_by}")
    await runner.tick()  # discovery, then the first window of <= 20 groups
    batch = next(b for _, b, s in store.windows[cid] if s == "active")
    store.batches[batch].state = "running"
    store.batches[batch].items[store.batches[batch].urls[3]] = "running"
    await runner.tick()
    store.batches[batch].state = "human_verification_required"
    store.profile = "human_verification_required"
    await runner.tick()
    store.batches[batch].state = "queued"
    store.profile = "ready"
    await runner.tick()
    store.finish_batch(batch)
    store.normalised[cid] = 2  # posts collected, analysis still running
    await runner.tick()
    store.add_finding(cid, "f1", "🏠 Квартира, 2 комнаты")
    clock.advance(121)
    await runner.tick()
    store.normalised[cid] = 0
    clock.advance(600)
    await runner.tick()
    await runner.tick()
    return cid


# --- the pure mapping ----------------------------------------------------------------------


def test_every_stage_maps_to_an_allowed_label() -> None:
    assert user_status("planning") == user_status("discovery") == SEARCHING
    assert user_status("facebook") == FACEBOOK
    assert user_status("web") == SEARCHING  # no site known yet: no place to name
    assert user_status("checking") == CHECKING
    assert user_status("finished", found=3) == "✅ Готово: отправлено 3"
    assert user_status("finished", found=0) == NOTHING
    assert user_status("verification", facebook_started=True) == FACEBOOK
    assert user_status("error") == SEARCHING
    assert user_status("site", site="https://www.idealista.com/alquiler/madrid") == (
        '🔎 Сейчас ищу на сайте <a href="https://idealista.com/">idealista.com</a>')
    assert user_status("site", site="<b>batch 20</b> / window") == SEARCHING
    assert user_status("site") == SEARCHING
    for state in ("planned", "discovering", "running", "paused_verification", "completed", "cancelled", "failed", "??"):
        for found in (0, 1):
            for checking in (False, True):
                label = campaign_label(state, found=found, checking=checking)
                assert label in ALLOWED and is_user_status(label)
    assert is_user_status(site_line("fotocasa.es"))
    assert not is_user_status("🔎 Сейчас ищу на сайте окно 20")
    assert not is_user_status("Ищу в интернете…")
    assert not is_user_status("Сейчас: Facebook · окно 1 ждёт запуска")


# --- the runner -----------------------------------------------------------------------------


async def test_a_normal_user_sees_only_allowed_statuses_through_a_whole_search() -> None:
    campaigns, store, messenger, clock, runner, plan = build()
    cid = await full_search(runner, campaigns, store, clock, plan, USER)
    assert (await campaigns.get(cid)).state == "completed"
    shown = statuses(messenger)
    assert shown and all(text in ALLOWED or text == GROUP_4 for text in shown), shown
    for text in shown:
        assert_user_safe(text)
    assert timeline(messenger) == [SEARCHING, FACEBOOK, GROUP_4, FACEBOOK, CHECKING, "✅ Готово: отправлено 1"]
    sent_statuses = [t for _, mid, t in messenger.sent if mid in messenger.status_ids or "🔎" not in t]
    # One status message at a time: it moved below the card and the old one was deleted.
    assert len(sent_statuses) - len(messenger.deleted) == 1, (sent_statuses, messenger.deleted)
    assert messenger.findings() == ["🏠 Квартира, 2 комнаты\n\n🔎 Найдено: 1 · ищу дальше"]


async def test_a_search_without_findings_ends_with_nothing_found() -> None:
    campaigns, store, messenger, clock, runner, plan = build()
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="telegram:7:auto")
    await runner.tick()
    store.finish_batch(next(b for _, b, s in store.windows[cid] if s == "active"))
    await runner.tick()
    clock.advance(121)
    await runner.tick()
    assert (await campaigns.get(cid)).state == "completed"
    assert statuses(messenger)[-1] == NOTHING
    assert all(t in ALLOWED for t in statuses(messenger))


async def test_pauses_refusals_cancel_and_errors_stay_user_safe() -> None:
    from bot.campaign.status_text import LIMIT_NOTE

    campaigns, store, messenger, _clock, runner, plan = build()
    breaker = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="telegram:7")
    store.breaker = "safety breaker open: 2 facebook challenges in the last 6 h"
    await runner.tick()
    # Facebook cannot be read today and nothing else runs: the search ends and says why, in plain words.
    assert statuses(messenger)[-1] == f"{NOTHING}\n{LIMIT_NOTE}"
    assert (await campaigns.get(breaker)).state == "completed"
    store.breaker = None

    quota = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="telegram:7")
    store.profile = "in_use"
    await runner.tick()
    store.profile = "ready"
    store.refusal = "daily quota reached: 6 of 6 Facebook batches in 24 h"
    await runner.tick()
    assert statuses(messenger)[-1] == f"{NOTHING}\n{LIMIT_NOTE}"
    assert (await campaigns.get(quota)).stop_reason == "facebook_daily_limit"
    store.refusal = None

    cancelled = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="telegram:7")
    await runner.tick()
    await campaigns.cancel(cancelled, "telegram:7")
    await runner.tick()
    assert statuses(messenger)[-1] == NOTHING

    other = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="telegram:7")
    await campaigns.set_state(other, "failed", "test", reason="runner_error:RuntimeError")
    await runner.tick()
    for text in statuses(messenger):
        assert is_user_status(text), text
        assert_user_safe(text)


async def test_the_owner_still_sees_the_technical_status() -> None:
    campaigns, store, messenger, clock, runner, plan = build()
    await full_search(runner, campaigns, store, clock, plan, OWNER)
    shown = statuses(messenger)
    assert all(t.startswith(f"🎯 {plan.goal}\n") for t in shown)
    assert any("Сейчас: Facebook · Group 4 · ищу дальше" in t for t in shown)
    assert any(t.endswith("Нужна verification") for t in shown)
    assert shown[-1].endswith("Кампания завершена · найдено 1")


async def test_a_failing_status_edit_never_breaks_the_run() -> None:
    campaigns, store, messenger, clock, runner, plan = build(messenger=FlakyMessenger())
    cid = await full_search(runner, campaigns, store, clock, plan, USER)
    assert (await campaigns.get(cid)).state == "completed"
    assert messenger.findings() == ["🏠 Квартира, 2 комнаты\n\n🔎 Найдено: 1 · ищу дальше"]
    # Edits fail, but the status still moved below at each phase change (sent anew, the old one deleted).
    assert [t for _, mid, t in messenger.sent if mid in messenger.status_ids or "🔎" not in t] == [
        SEARCHING, CHECKING, "✅ Готово: отправлено 1"]
    assert len(messenger.deleted) == 2
    assert messenger.summaries() == []  # «Итог поиска» is for owners only


# --- the Orchestra's /campaign notices -----------------------------------------------------


class BrokenCampaigns(MemoryCampaignStore):
    async def create(self, *args: object, **kwargs: object) -> str:
        raise RuntimeError("database unavailable at 10.0.0.5")


async def dispatch(arguments: str, campaigns: MemoryCampaignStore, roles: OperatorSet) -> list[tuple[int, str]]:
    store = FakeStore([claimed("campaign", arguments)])  # chat 10, user 20
    notices: list[tuple[int, str]] = []

    async def notify(chat: int, text: str) -> None:
        notices.append((chat, text))

    dispatcher = OrchestraDispatcher(store, operator_ids=roles.controllers, notifier=notify,  # type: ignore[arg-type]
                                     campaigns=campaigns, roles=roles)
    assert await dispatcher.process_once()
    return notices


@pytest.mark.parametrize("role", ["user", "operator"])
async def test_campaign_notices_to_a_non_owner_are_user_safe(role: str) -> None:
    roles = OperatorSet({OWNER}, {20: role})
    campaigns = MemoryCampaignStore()
    assert await dispatch(GOAL, campaigns, roles) == [], "the runner's status message says it"
    (campaign,) = campaigns.campaigns.values()

    notices = await dispatch("status", campaigns, roles)
    assert notices == [(10, SEARCHING)]
    notices = await dispatch(f"cancel {campaign.id}", campaigns, roles)
    # A user was already told «Поиск остановлен.» by the control plane; an operator hears it here.
    assert notices == ([] if role == "user" else [(10, DONE)])
    assert (await campaigns.get(campaign.id)).state == "cancelled"

    notices = await dispatch(GOAL, BrokenCampaigns(), roles)
    assert [n for n in notices if n[0] == 10] == [(10, NOTHING)]
    owner_notices = [t for chat, t in notices if chat == OWNER]
    assert len(owner_notices) == 1 and "failed safely" in owner_notices[0] and "telegram:20" in owner_notices[0]
    for chat, text in notices:
        if chat == 10:
            assert text in ALLOWED
            assert_user_safe(text)


async def test_campaign_notices_to_the_owner_stay_technical() -> None:
    roles = OperatorSet({20})
    campaigns = MemoryCampaignStore()
    notices = await dispatch(GOAL, campaigns, roles)
    (campaign,) = campaigns.campaigns.values()
    assert notices[0] == (10, "Orchestra: processing campaign request cmd-1.")
    assert notices[-1] == (10, f"Кампания {campaign.id} запланирована: {campaign.plan.goal}")


# --- social networks (bot.social_search) -----------------------------------------------------


def test_social_lines_link_the_query_and_carry_no_technical_text() -> None:
    from bot.campaign.status_text import INSTAGRAM, LINKEDIN, TIKTOK

    assert (TIKTOK, INSTAGRAM, LINKEDIN) == ("Ищу в TikTok…", "Ищу в Instagram…", "Ищу в LinkedIn…")
    assert social_line("tiktok", "terrenomadrid", "https://www.tiktok.com/tag/terrenomadrid") == (
        '🔎 Сейчас ищу в TikTok: <a href="https://www.tiktok.com/tag/terrenomadrid">terrenomadrid</a>')
    assert social_line("linkedin", "business angel & Madrid") == (
        '🔎 Сейчас ищу в LinkedIn: <a href="https://www.linkedin.com/search/results/content/?keywords='
        'business+angel+%26+Madrid">business angel &amp; Madrid</a>')
    assert social_line("instagram") == '🔎 Сейчас ищу в <a href="https://www.instagram.com/">Instagram</a>'
    assert social_line("myspace", "x") is None
    for platform in ("tiktok", "instagram", "linkedin"):
        for line in (social_line(platform), social_line(platform, "запрос <b>")):
            assert line is not None and is_user_status(line) and "<b>" not in line
            assert_user_safe(line)
        assert user_status("social", platform=platform) == social_line(platform)
        # Checking a finding and the end of the search win over the network label.
        assert campaign_label("running", social=platform, checking=True) == CHECKING
        assert campaign_label("completed", social=platform, found=1) == "✅ Готово: отправлено 1"
    assert user_status("social", platform="myspace") == SEARCHING
    assert campaign_label("running", social="myspace") == FACEBOOK


async def test_a_user_sees_the_network_while_facebook_is_idle_and_owners_see_the_query_and_notes() -> None:
    from bot.campaign.runs import SocialActivity

    campaigns, store, messenger, _clock, runner, plan = build()
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="telegram:7")
    await runner.tick()  # discovery, first window waits for facebook-runner
    store.social[cid] = SocialActivity(searching="tiktok", query="terrenomadrid", pending=True,
                                       url="https://www.tiktok.com/tag/terrenomadrid",
                                       notes=("instagram: нет готового профиля — войдите через /login instagram",))
    await runner.tick()
    # The network search is the newest change: it is the one place shown (Facebook has no group read yet).
    assert timeline(messenger)[-1] == (
        '🔎 Сейчас ищу в TikTok: <a href="https://www.tiktok.com/tag/terrenomadrid">terrenomadrid</a>')
    store.finish_batch(next(b for _, b, s in store.windows[cid] if s == "active"))
    await runner.tick()  # the window closed; Facebook idles through the cooldown
    assert "tiktok.com/tag/terrenomadrid" in timeline(messenger)[-1]
    for text in statuses(messenger):
        assert text in ALLOWED or is_user_status(text), text
        assert "профил" not in text

    owner_campaigns, owner_store, owner_messenger, _, owner_runner, owner_plan = build()
    oid = await owner_campaigns.create(owner_plan, chat_id=CHAT, requested_by=OWNER, source_text=GOAL, actor="t")
    owner_store.social[oid] = SocialActivity(searching="linkedin", query="business angel Madrid", pending=True,
                                             notes=("tiktok: нет готового профиля — войдите через /login tiktok",))
    await owner_runner.tick()
    shown = timeline(owner_messenger)[-1]
    assert "Соцсети: linkedin · «business angel Madrid»" in shown and "/login tiktok" in shown


def test_the_group_line_names_the_group_with_its_link_and_escapes_everything() -> None:
    from bot.campaign.status_text import group_line

    url = "https://www.facebook.com/groups/pisos/"
    assert group_line("Pisos en Madrid · alquiler", url) == (
        f'🔎 Сейчас ищу в Facebook в группе <a href="{url}">Pisos en Madrid · alquiler</a>')
    assert is_user_status(group_line("Недвижимость Испании", url))
    for junk in (None, "", "123456789012345", "https://www.facebook.com/groups/x"):  # no readable name:
        assert group_line(junk, url) == f'🔎 Сейчас ищу в Facebook в <a href="{url}">группе</a>', junk  # «группе» links
    hostile = group_line("<b>x</b>", url)
    assert hostile == f'🔎 Сейчас ищу в Facebook в группе <a href="{url}">&lt;b&gt;x&lt;/b&gt;</a>' and is_user_status(hostile)
    long = group_line("Квартиры " * 20, url)
    assert long.count("…") == 1 and len(long) < 160 and is_user_status(long)
    assert len(re.search(r">([^<]+)</a>", long).group(1)) == 60
    # a link that is not http(s) is dropped, the name stays
    assert group_line("Pisos", "javascript:alert(1)") == "🔎 Сейчас ищу в Facebook в группе Pisos"
    assert not is_user_status('🔎 Сейчас ищу в Facebook в группе <a href="https://x">a<b></a>')


def test_site_investor_and_link_lines() -> None:
    from bot.campaign.status_text import reach_line

    # the page being read now is the link; the text is the host without www.
    assert site_line("www.Idealista.com", "https://www.idealista.com/alquiler/madrid/") == (
        '🔎 Сейчас ищу на сайте <a href="https://www.idealista.com/alquiler/madrid/">idealista.com</a>')
    assert site_line("idealista.com") == '🔎 Сейчас ищу на сайте <a href="https://idealista.com/">idealista.com</a>'
    # a URL with quotes and markup is escaped inside the attribute
    nasty = site_line("x.es", 'https://x.es/a"b<c>&d')
    assert nasty == '🔎 Сейчас ищу на сайте <a href="https://x.es/a&quot;b&lt;c&gt;&amp;d">x.es</a>' and is_user_status(nasty)
    # a page of another site is not trusted for this host; no host and no URL means no place
    assert site_line("x.es", "https://evil.com/p") == '🔎 Сейчас ищу на сайте <a href="https://x.es/">x.es</a>'
    assert site_line(None, "https://pisos.com/a") == '🔎 Сейчас ищу на сайте <a href="https://pisos.com/a">pisos.com</a>'
    assert site_line("<b>x</b>", None) is None and site_line(None, None) is None
    assert site_line("x.es", "javascript:alert(1)") == '🔎 Сейчас ищу на сайте <a href="https://x.es/">x.es</a>'
    assert reach_line("linkedin") == '🔎 Сейчас ищу на <a href="https://linkedin.com/">linkedin.com</a>'
    assert reach_line("reddit", "https://www.reddit.com/r/a/comments/1/x") == (
        '🔎 Сейчас ищу на <a href="https://www.reddit.com/r/a/comments/1/x">reddit.com</a>')
    assert reach_line("reddit", "https://evil.com/") == '🔎 Сейчас ищу на <a href="https://reddit.com/">reddit.com</a>'
    assert reach_line("web") is None and reach_line(None) is None
    assert is_user_status(reach_line("x")) and is_user_status(CHECKING) and CHECKING == "🔎 Поиск завершён. Проверяю найденное…"


# --- the website stage's live line: only the place, with its link --------------------------------------


class Web:
    """A web stage fake: ``progress`` is what the worker publishes (the page read now), ``host`` the stored host."""

    def __init__(self, progress, *, active: bool = True, host: str | None = None) -> None:
        self.progress, self.active, self.host = progress, active, host
        self.line = "сайты: страниц 37/60 · сайт idealista.com"

    async def web_status(self, campaign_id: str):
        from bot.web_search.models import WebStatus

        return WebStatus(self.active, self.host if self.active else None, self.line, self.progress)


async def test_users_get_only_the_site_being_read_and_owners_also_the_technical_lines_with_counts() -> None:
    from bot.web_search.models import WebProgress
    from tests.test_campaign_runner import create, make

    campaigns, _, messenger, _, clock, runner, plan = make()
    web = Web(WebProgress("idealista.com", "browser", 37, 12, 4, 8, (("http", 3), ("browser", 1)),
                          url="https://www.idealista.com/alquiler-viviendas/madrid/?p=3&q=\"x\""), host="idealista.com")
    runner.web = web
    user_cid = await campaigns.create(plan, chat_id=-1, requested_by=8, source_text=GOAL, actor="telegram:8")
    owner_cid = await create(campaigns, plan)
    await runner.step(user_cid)
    await runner.step(owner_cid)
    user_text = next(t for c, _, t in messenger.sent if c == -1)
    link = 'https://www.idealista.com/alquiler-viviendas/madrid/?p=3&amp;q=&quot;x&quot;'
    assert user_text == f'🔎 Сейчас ищу на сайте <a href="{link}">idealista.com</a>'
    assert not any(word in user_text for word in ("прочитано", "найдено", "порталов", "Сейчас: сайты", "Ищу в интернете"))
    assert is_user_status(user_text)
    owner_text = next(t for c, _, t in messenger.sent if c == CHAT)
    lines = owner_text.split("\n")
    assert lines[0].startswith("🎯 ") and lines[1] == user_text  # the new line comes first, under the goal
    assert "сайты: страниц 37/60" in owner_text and "idealista.com: отказы напрямую 3, браузер 1" in owner_text
    # every status message is HTML, with no link preview; they are the only HTML messages
    assert set(messenger.modes) == {"HTML"} and len(messenger.modes) == 2

    # the next page of the same site: one edit per web_status_seconds
    web.progress = WebProgress("idealista.com", "http", 40, 13, 4, 8, url="https://www.idealista.com/a/2")
    await runner.step(user_cid)
    assert messenger.edits == []  # throttled: the change waits for the next allowed edit
    clock.advance(11)
    await runner.step(user_cid)
    assert len(messenger.edits) == 1 and messenger.edits[-1][2] == (
        '🔎 Сейчас ищу на сайте <a href="https://www.idealista.com/a/2">idealista.com</a>')
    assert messenger.modes[-1] == "HTML"

    # another site: the line changes at the next allowed edit, never earlier
    web.progress, web.host = WebProgress("fotocasa.es", "http", 41, 13, 4, 8, url="https://www.fotocasa.es/es/"), "fotocasa.es"
    await runner.step(user_cid)
    assert len(messenger.edits) == 1
    clock.advance(11)
    await runner.step(user_cid)
    assert "fotocasa.es</a>" in messenger.edits[-1][2] and len(messenger.edits) == 2
    await runner.step(user_cid)
    assert len(messenger.edits) == 2  # the same line again: nothing to edit

    # the web stage ended while Facebook idles: no counts, only the plain label
    web.active, web.progress = False, WebProgress(None, None, 52, 17, 8, 8, finished=True)
    clock.advance(11)
    await runner.step(user_cid)
    assert messenger.edits[-1][2] == FACEBOOK and "52" not in messenger.edits[-1][2]


async def test_the_site_falls_back_to_the_stored_host_and_a_stage_without_a_site_has_no_place() -> None:
    from bot.web_search.models import WebProgress
    from tests.test_campaign_runner import make

    campaigns, _, _, _, _, runner, plan = make()
    cid = await campaigns.create(plan, chat_id=-1, requested_by=8, source_text=GOAL, actor="telegram:8")
    campaign = await campaigns.get(cid)
    runner.web = Web(None, host="www.fotocasa.es")  # a worker in another process: the progress is unknown
    assert await runner._status_text(campaign, "") == '🔎 Сейчас ищу на сайте <a href="https://fotocasa.es/">fotocasa.es</a>'
    runner.web = Web(WebProgress(None, None, 0, 0))  # planning queries: no site yet
    assert await runner._status_text(campaign, "") == SEARCHING
    runner.web = Web(WebProgress("<b>x</b>", "http", 1, 0, url="javascript:alert(1)"), host="<b>x</b>")
    assert await runner._status_text(campaign, "") == SEARCHING  # a hostile host is not a place


async def test_a_facebook_group_with_and_without_a_name_and_a_hostile_name_in_the_status() -> None:
    from bot.campaign.runs import Window
    from tests.test_campaign_runner import make

    campaigns, _, _, _, _, runner, plan = make()
    cid = await campaigns.create(plan, chat_id=-1, requested_by=8, source_text=GOAL, actor="telegram:8")
    campaign = await campaigns.get(cid)
    url = "https://www.facebook.com/groups/pisos/"
    for name, expect in (("Pisos Madrid", f'в группе <a href="{url}">Pisos Madrid</a>'),
                         ("<b>x</b>", f'в группе <a href="{url}">&lt;b&gt;x&lt;/b&gt;</a>'),
                         ("1234567890", f'в <a href="{url}">группе</a>')):
        runner._group_now[cid] = (name, url)
        text = await runner._status_text(campaign, f"Сейчас: Facebook · {name} · ищу дальше")
        assert text == f"🔎 Сейчас ищу в Facebook {expect}", text
    assert Window(1, "b", "running", "G", url).current_group_url == url


async def test_the_newest_change_wins_between_parallel_stages_and_judging_has_its_own_line() -> None:
    from bot.campaign.runner import ANALYSIS
    from bot.campaign.runs import ReachActivity, SocialActivity
    from bot.web_search.models import WebProgress
    from tests.test_campaign_runner import make

    campaigns, store, _, _, clock, runner, plan = make()
    cid = await campaigns.create(plan, chat_id=-1, requested_by=8, source_text=GOAL, actor="telegram:8")
    campaign = await campaigns.get(cid)
    web = runner.web = Web(WebProgress("pisos.com", "http", 1, 0, url="https://www.pisos.com/a"), host="pisos.com")
    def pisos(page: str) -> str:
        return f'🔎 Сейчас ищу на сайте <a href="https://www.pisos.com/{page}">pisos.com</a>'

    assert await runner._status_text(campaign, "") == pisos("a")
    clock.advance(5)
    store.social[cid] = SocialActivity(searching="tiktok", query="terreno madrid", url="https://www.tiktok.com/search?q=t")
    assert await runner._status_text(campaign, "") == (
        '🔎 Сейчас ищу в TikTok: <a href="https://www.tiktok.com/search?q=t">terreno madrid</a>')
    clock.advance(5)  # the site moves on: it changed last
    web.progress = WebProgress("pisos.com", "http", 2, 0, url="https://www.pisos.com/b")
    assert await runner._status_text(campaign, "") == pisos("b")
    clock.advance(5)
    store.reach[cid] = ReachActivity("linkedin", "site:linkedin.com/in inversor madrid")
    assert await runner._status_text(campaign, "") == '🔎 Сейчас ищу на <a href="https://linkedin.com/">linkedin.com</a>'
    clock.advance(5)
    store.normalised[cid] = 3  # posts are being judged, but a search stage is still active: the place wins
    web.progress = WebProgress("pisos.com", "http", 3, 0, url="https://www.pisos.com/c")
    assert await runner._status_text(campaign, ANALYSIS) == pisos("c")
    store.social.pop(cid), store.reach.pop(cid)
    web.active = False
    assert await runner._status_text(campaign, ANALYSIS) == "🔎 Поиск завершён. Проверяю найденное…"
    store.normalised[cid] = 0
    assert await runner._status_text(campaign, ANALYSIS) == SEARCHING  # nothing specific: the fixed label


async def test_the_telegram_messenger_sends_html_only_when_asked() -> None:
    import json

    import httpx

    from bot.campaign.runner import TelegramMessenger

    bodies: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path.rsplit("/", 1)[1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    messenger = TelegramMessenger("t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await messenger.send(1, "<b>card</b> 🏠")
    await messenger.edit(1, 5, "<b>card</b> 🏠")
    await messenger.send(1, "🔎 Сейчас ищу", parse_mode="HTML")
    await messenger.edit(1, 5, "🔎 Сейчас ищу", parse_mode="HTML")
    assert [m for m, _ in bodies] == ["sendMessage", "editMessageText", "sendMessage", "editMessageText"]
    assert all("parse_mode" not in body for _, body in bodies[:2])  # cards and questions stay plain text
    assert all(body["parse_mode"] == "HTML" for _, body in bodies[2:])
    assert all(body["disable_web_page_preview"] is True for _, body in bodies)
    await messenger.aclose()


# --- phases of the user line: searching -> checking -> done, re-posted at the bottom ------------------------------

CHECKING_3_OF_8 = "🔎 Поиск завершён. Проверяю найденное: проверено 3 из 8"


async def phase_show(runner: CampaignRunner, campaigns: MemoryCampaignStore, cid: str, line: str) -> None:
    await runner._show(await campaigns.get(cid), line)


async def phase_campaign(requested_by: int = USER, **config):
    campaigns, store, messenger, clock, runner, plan = build()
    if config:
        runner.config = RunnerConfig(relevance_fail_closed=False, **config)
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=requested_by, source_text=GOAL,
                                 actor=f"telegram:{requested_by}")
    return campaigns, store, messenger, clock, runner, cid


async def test_the_checking_line_shows_checked_of_collected_and_a_plain_one_without_posts() -> None:
    from bot.campaign.runner import ANALYSIS

    campaigns, store, messenger, clock, runner, cid = await phase_campaign()
    store.normalised[cid] = 5
    await phase_show(runner, campaigns, cid, ANALYSIS)  # nothing collected yet: total 0
    assert messenger.sent[-1][2] == "🔎 Поиск завершён. Проверяю найденное…"
    clock.advance(30)
    store.progress[cid] = (3, 8)
    await phase_show(runner, campaigns, cid, ANALYSIS)
    shown = messenger.timeline[-1]
    assert shown == CHECKING_3_OF_8 and is_user_status(shown)
    assert (await campaigns.get(cid)).status_message_id == len(messenger.sent)  # same phase: edited, not re-posted
    assert len(messenger.deleted) == 0
    store.progress[cid] = (9, 8)  # never more checked than collected
    clock.advance(30)
    await phase_show(runner, campaigns, cid, ANALYSIS)
    assert messenger.timeline[-1].endswith("проверено 8 из 8")


async def test_a_phase_change_deletes_the_old_status_and_sends_the_new_one_below() -> None:
    from bot.campaign.runner import ANALYSIS

    campaigns, store, messenger, clock, runner, cid = await phase_campaign()
    await phase_show(runner, campaigns, cid, "")
    first = (await campaigns.get(cid)).status_message_id
    assert messenger.sent[-1][2] == SEARCHING
    clock.advance(25)
    store.normalised[cid], store.progress[cid] = 5, (3, 8)
    await phase_show(runner, campaigns, cid, ANALYSIS)  # searching -> checking
    second = (await campaigns.get(cid)).status_message_id
    assert second != first and messenger.deleted == [(CHAT, first)]
    assert messenger.sent[-1] == (CHAT, second, CHECKING_3_OF_8) and messenger.edits == []
    clock.advance(25)
    store.normalised[cid] = 0
    store.streamed[cid] = {"a": 1, "b": 2}
    await campaigns.set_state(cid, "running", "campaign:runner")
    await campaigns.set_state(cid, "completed", "campaign:runner")
    await phase_show(runner, campaigns, cid, "Кампания завершена")  # checking -> done
    third = (await campaigns.get(cid)).status_message_id
    assert messenger.deleted == [(CHAT, first), (CHAT, second)] and third not in (first, second)
    assert messenger.sent[-1][2] == "✅ Готово: отправлено 2" and is_user_status(messenger.sent[-1][2])
    assert (await store.get_run(cid)).status_text == "✅ Готово: отправлено 2"


async def test_the_done_line_without_findings_keeps_the_nothing_found_wording() -> None:
    campaigns, _store, messenger, clock, runner, cid = await phase_campaign()
    await phase_show(runner, campaigns, cid, "")
    clock.advance(25)
    await campaigns.set_state(cid, "running", "campaign:runner")
    await campaigns.set_state(cid, "completed", "campaign:runner")
    await phase_show(runner, campaigns, cid, "Кампания завершена")
    assert messenger.sent[-1][2] == "Пока ничего подходящего не нашёл."


async def test_every_fifth_card_reposts_the_status_below() -> None:
    campaigns, _store, messenger, clock, runner, cid = await phase_campaign()
    await phase_show(runner, campaigns, cid, "")
    first = (await campaigns.get(cid)).status_message_id
    clock.advance(25)
    runner._cards_since[cid] = 4
    await phase_show(runner, campaigns, cid, "")
    assert messenger.deleted == [] and (await campaigns.get(cid)).status_message_id == first
    runner._cards_since[cid] = 5
    await phase_show(runner, campaigns, cid, "")  # same text, still re-posted
    assert messenger.deleted == [(CHAT, first)] and (await campaigns.get(cid)).status_message_id != first
    assert runner._cards_since[cid] == 0


async def test_a_repost_happens_at_most_once_per_twenty_seconds() -> None:
    campaigns, store, messenger, clock, runner, cid = await phase_campaign()
    await phase_show(runner, campaigns, cid, "")
    clock.advance(25)
    runner._cards_since[cid] = 5
    await phase_show(runner, campaigns, cid, "")
    assert len(messenger.deleted) == 1
    clock.advance(10)
    runner._cards_since[cid] = 5
    store.normalised[cid] = 1
    from bot.campaign.runner import ANALYSIS
    await phase_show(runner, campaigns, cid, ANALYSIS)  # a phase change too, but under 20 s: edited in place
    assert len(messenger.deleted) == 1 and messenger.edits and "Проверяю найденное" in messenger.edits[-1][2]
    clock.advance(11)
    await phase_show(runner, campaigns, cid, ANALYSIS)  # the floor passed: the waiting re-post happens
    assert len(messenger.deleted) == 2 and "Проверяю найденное" in messenger.sent[-1][2]


async def test_a_failing_delete_still_sends_the_new_status() -> None:
    from bot.campaign.runner import ANALYSIS

    class NoDelete(FakeMessenger):
        async def delete(self, chat_id: int, message_id: int) -> None:
            raise RuntimeError("message to delete not found")

    campaigns, store, _, clock, runner, plan = build(messenger=NoDelete())
    messenger = runner.messenger
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor=f"telegram:{USER}")
    await phase_show(runner, campaigns, cid, "")
    first = (await campaigns.get(cid)).status_message_id
    clock.advance(25)
    store.normalised[cid] = 2
    await phase_show(runner, campaigns, cid, ANALYSIS)
    assert (await campaigns.get(cid)).status_message_id != first
    assert messenger.sent[-1][2] == "🔎 Поиск завершён. Проверяю найденное…"


async def test_owners_keep_their_technical_lines_below_the_user_line_in_every_phase() -> None:
    from bot.campaign.runner import ANALYSIS

    campaigns, store, messenger, clock, runner, cid = await phase_campaign(OWNER)
    await phase_show(runner, campaigns, cid, "")
    clock.advance(25)
    store.normalised[cid], store.progress[cid] = 5, (3, 8)
    await phase_show(runner, campaigns, cid, ANALYSIS)
    lines = messenger.sent[-1][2].split("\n")
    assert lines[0].startswith("🎯 ") and lines[1] == CHECKING_3_OF_8 and lines[2] == ANALYSIS
    assert messenger.deleted  # the owner's message is re-posted on the phase change as well


async def test_html_in_the_checking_and_done_lines_stays_escaped_for_owners() -> None:
    from bot.campaign.runner import ANALYSIS

    campaigns, store, messenger, _clock, runner, cid = await phase_campaign(OWNER)
    store.normalised[cid] = 1
    await phase_show(runner, campaigns, cid, ANALYSIS + " <b>x</b> & y")
    text = messenger.sent[-1][2]
    assert "<b>" not in text and "&lt;b&gt;x&lt;/b&gt; &amp; y" in text
