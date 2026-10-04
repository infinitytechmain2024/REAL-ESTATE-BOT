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
    WEB,
    campaign_label,
    is_user_status,
    site_status,
    user_status,
)
from bot.operators import OperatorSet
from bot.orchestra.dispatcher import OrchestraDispatcher
from tests.test_campaign_runner import GOAL, SUMMARY, Clock, FakeDiscovery, FakeMessenger
from tests.test_orchestra_dispatcher import FakeStore, claimed

USER, OWNER, CHAT = 7, 99, -100
ALLOWED = {"Принято. Начинаю поиск.", "Ищу…", "Ищу в Facebook…", "Ищу в интернете…", "Нашёл вариант, проверяю…",
           "Поиск завершён.", "Пока ничего подходящего не нашёл.",
           "Ищу в TikTok…", "Ищу в Instagram…", "Ищу в LinkedIn…"}
GROUP_4 = "Ищу в группе Facebook «Group 4»…"  # the group being read, by its name
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", re.I)
FORBIDDEN = ("window", "окно", "20", "batch", "campaign", "Кампания", "кампания", "orchestra", "Orchestra",
             "/campaign", "групп", "Queue", "Пауза", "verification", "🎯")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def assert_user_safe(text: str) -> None:
    if text.startswith("Ищу в группе Facebook «"):  # the group's own name, checked by group_status
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

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        raise RuntimeError("telegram 400: Bad Request: message is not modified")


def build(*, owners: frozenset[int] = frozenset({OWNER}), groups: int = 20, messenger: FakeMessenger | None = None):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = messenger or FakeMessenger()
    discovery = FakeDiscovery(campaigns, store, groups)
    clock = Clock()
    runner = CampaignRunner(campaigns, store, messenger, discovery, now=clock, owner_ids=owners,
                            config=RunnerConfig(window_cooldown_seconds=120, analysis_grace_seconds=600))
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
    assert user_status("web") == WEB
    assert user_status("checking") == CHECKING
    assert user_status("finished", found=3) == DONE
    assert user_status("finished", found=0) == NOTHING
    assert user_status("verification", facebook_started=True) == FACEBOOK
    assert user_status("error") == SEARCHING
    assert user_status("site", site="https://www.idealista.com/alquiler/madrid") == "Ищу на сайте idealista.com…"
    assert user_status("site", site="<b>batch 20</b> / window") == WEB
    assert user_status("site") == WEB
    for state in ("planned", "discovering", "running", "paused_verification", "completed", "cancelled", "failed", "??"):
        for found in (0, 1):
            for checking in (False, True):
                label = campaign_label(state, found=found, checking=checking)
                assert label in ALLOWED and is_user_status(label)
    assert is_user_status(site_status("fotocasa.es"))
    assert not is_user_status("Ищу на сайте окно 20…")
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
    assert timeline(messenger) == [SEARCHING, FACEBOOK, GROUP_4, FACEBOOK, CHECKING, DONE]
    sent_statuses = [t for _, _, t in messenger.sent if "🔎" not in t and not t.startswith(SUMMARY)]
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
    # Edits fail, but the status still moved below the card and the summary (sent anew, the old one deleted).
    assert [t for _, _, t in messenger.sent if "🔎" not in t and not t.startswith(SUMMARY)] == [
        SEARCHING, CHECKING, DONE]
    assert len(messenger.deleted) == 2
    assert len(messenger.summaries()) == 1


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


def test_social_labels_are_allowed_and_carry_no_technical_text() -> None:
    from bot.campaign.status_text import INSTAGRAM, LINKEDIN, TIKTOK

    assert (TIKTOK, INSTAGRAM, LINKEDIN) == ("Ищу в TikTok…", "Ищу в Instagram…", "Ищу в LinkedIn…")
    for platform, label in (("tiktok", TIKTOK), ("instagram", INSTAGRAM), ("linkedin", LINKEDIN)):
        assert user_status("social", platform=platform) == label
        assert campaign_label("running", social=platform) == label and is_user_status(label)
        assert_user_safe(label)
        # Checking a finding and the end of the search win over the network label.
        assert campaign_label("running", social=platform, checking=True) == CHECKING
        assert campaign_label("completed", social=platform, found=1) == DONE
    assert user_status("social", platform="myspace") == SEARCHING
    assert campaign_label("running", social="myspace") == FACEBOOK
    assert not is_user_status("Ищу в TikTok · #terrenomadrid…")


async def test_a_user_sees_the_network_while_facebook_is_idle_and_owners_see_the_query_and_notes() -> None:
    from bot.campaign.runs import SocialActivity

    campaigns, store, messenger, _clock, runner, plan = build()
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="telegram:7")
    await runner.tick()  # discovery, first window waits for facebook-runner
    store.social[cid] = SocialActivity(searching="tiktok", query="terrenomadrid", pending=True,
                                       notes=("instagram: нет готового профиля — войдите через /login instagram",))
    await runner.tick()
    assert timeline(messenger)[-1] == "Ищу в Facebook…"  # Facebook reads a window: its label stays
    store.finish_batch(next(b for _, b, s in store.windows[cid] if s == "active"))
    await runner.tick()  # the window closed; Facebook idles through the cooldown
    assert timeline(messenger)[-1] == "Ищу в TikTok…"
    for text in statuses(messenger):
        assert text in ALLOWED, text
        assert "профил" not in text and "terrenomadrid" not in text

    owner_campaigns, owner_store, owner_messenger, _, owner_runner, owner_plan = build()
    oid = await owner_campaigns.create(owner_plan, chat_id=CHAT, requested_by=OWNER, source_text=GOAL, actor="t")
    owner_store.social[oid] = SocialActivity(searching="linkedin", query="business angel Madrid", pending=True,
                                             notes=("tiktok: нет готового профиля — войдите через /login tiktok",))
    await owner_runner.tick()
    shown = timeline(owner_messenger)[-1]
    assert "Соцсети: linkedin · «business angel Madrid»" in shown and "/login tiktok" in shown


def test_the_group_being_read_is_named_only_when_the_name_is_safe() -> None:
    from bot.campaign.status_text import group_status

    assert group_status("Pisos en Madrid · alquiler") == "Ищу в группе Facebook «Pisos en Madrid · alquiler»…"
    assert is_user_status(group_status("Недвижимость Испании"))
    for junk in (None, "", "123456789012345", "https://www.facebook.com/groups/x", "<b>x</b>", "a/b", "«x»"):
        assert group_status(junk) == FACEBOOK, junk
    long = group_status("Квартиры " * 20)
    assert long.endswith("…»…") and len(long) < 100 and is_user_status(long)
    assert not is_user_status("Ищу в группе Facebook «https://x»…")
