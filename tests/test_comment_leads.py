"""Investor leads from the comments under sent objects: parsing, judging, the worker, the runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.leads import (
    Comment,
    CommentConfig,
    CommentLeadWorker,
    CommentRead,
    MemoryLeadStore,
    Person,
    PostContext,
    Sighting,
    Verdict,
    contacts_in,
    is_facebook_post,
    object_facts,
    parse_comments,
    parse_verdicts,
    person_card,
    profile_link,
    rule_verdict,
)
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.facebook_collector.browser import BrowserLease
from tests.test_campaign_runner import CHAT, Clock, FakeMessenger

POST = "https://www.facebook.com/groups/123/posts/456/"
LAND = PostContext("real_estate", "участок в Мадриде до 60 000 €", "Продаётся участок 2000 м² в Мадриде")


# --- links and comments ----------------------------------------------------------------------------


@pytest.mark.parametrize(("href", "expected"), [
    ("https://www.facebook.com/groups/123/user/100012345678/?__cft__[0]=x",
     ("fb:100012345678", "https://www.facebook.com/profile.php?id=100012345678")),
    ("https://m.facebook.com/profile.php?id=100099&__tn__=R",
     ("fb:100099", "https://www.facebook.com/profile.php?id=100099")),
    ("https://www.facebook.com/people/Juan-Perez/100077777/", ("fb:100077777", "https://www.facebook.com/profile.php?id=100077777")),
    ("https://www.facebook.com/Olga.Invest?__tn__=R", ("fb:olga.invest", "https://www.facebook.com/Olga.Invest")),
    ("https://www.facebook.com/groups/123/", None),
    ("https://www.facebook.com/groups/123/posts/456/?comment_id=1", None),
    ("https://www.facebook.com/hashtag/terreno", None),
    ("https://www.facebook.com/photo.php?fbid=1", None),
    ("https://www.facebook.com/profile.php?id=abc", None),
    ("https://evil.example/olga.invest", None),
    (None, None),
])
def test_profile_links_are_canonical_and_only_people_or_pages(href, expected) -> None:
    assert profile_link(href) == expected


def test_only_single_facebook_posts_have_their_comments_read() -> None:
    assert is_facebook_post(POST)
    assert is_facebook_post("https://www.facebook.com/groups/madrid/permalink/789/")
    assert is_facebook_post("https://www.facebook.com/somepage/posts/pfbid0abc")
    assert not is_facebook_post("https://www.facebook.com/groups/123/")
    assert not is_facebook_post("https://www.idealista.com/inmueble/1/")
    assert not is_facebook_post("http://www.facebook.com/groups/123/posts/456/")
    assert not is_facebook_post(None)


def test_comments_need_a_profile_and_text_each_once_without_the_posts_author() -> None:
    raw = [
        {"author": "Juan Pérez", "author_url": "https://www.facebook.com/groups/1/user/100012345678/",
         "text": "Me interesa  ¿precio?", "comment_url": "https://www.facebook.com/groups/1/posts/2/?comment_id=3"},
        {"author": "Juan Pérez", "author_url": "https://www.facebook.com/groups/1/user/100012345678/",
         "text": "me interesa ¿PRECIO?"},                                                          # the same again
        {"author": "Vendedor", "author_url": "https://www.facebook.com/groups/1/user/100055555555/",
         "text": "Te escribo por privado"},                                                        # the seller
        {"author": "Nadie", "author_url": "https://www.facebook.com/groups/1/", "text": "hola"},     # not a person
        {"author": "Ana", "author_url": "https://www.facebook.com/ana.garcia", "text": ""},          # no text
        "junk",
    ]
    comments = parse_comments(raw, post_author="https://www.facebook.com/groups/1/user/100055555555/?x=1")
    assert [(c.author, c.profile_key, c.text) for c in comments] == [
        ("Juan Pérez", "fb:100012345678", "Me interesa ¿precio?")]
    assert comments[0].comment_url == "https://www.facebook.com/groups/1/posts/2/?comment_id=3"
    assert parse_comments(None) == [] and parse_comments({"a": 1}) == []


# --- who is a lead ----------------------------------------------------------------------------------


def _comment(text: str) -> Comment:
    return Comment("X", "fb:1", "https://www.facebook.com/profile.php?id=1", text)


@pytest.mark.parametrize(("text", "role"), [
    ("Me interesa, ¿precio final?", "buyer"),
    ("Интересно, напишите в лс", "buyer"),
    ("Soy inversor, busco rentabilidad en la zona", "investor"),
    ("Инвестирую в землю под Мадридом", "investor"),
    ("Vendo otra parcela más barata, mira https://example.com", "seller"),
    ("@Maria mira esto", "other"),
])
def test_the_rules_tell_buyers_and_investors_from_sellers_and_the_rest(text: str, role: str) -> None:
    assert rule_verdict(_comment(text), LAND).role == role


def test_on_a_rent_search_an_interested_commenter_is_a_tenant_not_a_lead() -> None:
    rent = PostContext("real_estate", "квартира в аренду в Мадриде", "Сдаётся квартира")
    verdict = rule_verdict(_comment("Me interesa, ¿disponible?"), rent)
    assert verdict.role == "tenant" and not verdict.lead


def test_only_confident_investors_and_buyers_are_leads() -> None:
    assert Verdict("investor", 0.8).lead and Verdict("buyer", 0.6).lead
    assert not Verdict("buyer", 0.59).lead and not Verdict("seller", 0.99).lead and not Verdict("other", 1).lead


def test_the_models_answer_maps_to_one_verdict_per_comment_and_drift_is_other() -> None:
    content = "```json\n" + json.dumps({"comments": [
        {"index": 1, "role": "Investor", "confidence": 0.9, "summary_ru": "Ищет  объекты для вложений"},
        {"index": 0, "role": "buyer", "confidence": "7", "summary_ru": ""},
        {"index": 5, "role": "buyer", "confidence": 1, "summary_ru": "out of range"},
        {"index": 2, "role": "boss", "confidence": 0.9, "summary_ru": "?"},
    ]}) + "\n```"
    verdicts = parse_verdicts(content, 3)
    assert verdicts[0] == Verdict("buyer", 1.0, None)
    assert verdicts[1] == Verdict("investor", 0.9, "Ищет объекты для вложений")
    assert verdicts[2].role == "other"


# --- the person card --------------------------------------------------------------------------------

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
LAND_FACTS = {"property_type": "land", "price_amount": 50_000, "price_currency": "EUR", "area_m2": 2000}


def _person(*sightings: Sighting, name: str | None = "Juan Pérez") -> Person:
    return Person("fb:100012345678", "https://www.facebook.com/profile.php?id=100012345678", name, sightings)


def test_the_person_card_has_the_profile_contacts_preview_advice_and_every_object() -> None:
    card = person_card(_person(
        Sighting(POST, "Me interesa, llámame al +34 612 345 678", "buyer", NOW - timedelta(days=3),
                 "Хочет купить участок", LAND_FACTS),
        Sighting("https://www.facebook.com/groups/1/posts/9/", "¿Sigue disponible?", "buyer", NOW - timedelta(days=9),
                 None, {"property_type": "house", "price_amount": 180_000, "price_currency": "EUR"}),
    ), location="Madrid", now=NOW)
    assert card.splitlines() == [
        "🏠 Хочет купить недвижимость",
        "Имя: Juan Pérez",
        "Профиль: https://www.facebook.com/profile.php?id=100012345678",
        "Контакт из комментариев: +34 612 345 678",
        "Превью: Интерес к 2 объектам (Мадрид): участки, дома; цены объектов 50 000 € – 180 000 €; "
        "последний комментарий 3 дн. назад.",
        "Что хочет: Хочет купить участок",
        "Рекомендация: В комментарии есть контакт: можно связаться напрямую.",
        "Комментарии под объектами:",
        "1. Участок · 2 000 м² · 50 000 € — «Me interesa, llámame al +34 612 345 678»",
        POST,
        "2. Дом · 180 000 € — «¿Sigue disponible?»",
        "https://www.facebook.com/groups/1/posts/9/",
    ]


def test_an_investor_is_named_so_and_advice_follows_what_they_wrote() -> None:
    investor = person_card(_person(Sighting(POST, "Soy inversor", "investor", NOW, None, LAND_FACTS),
                                   Sighting(POST + "x", "Me interesa", "buyer", NOW, None, {})), location="Madrid", now=NOW)
    assert investor.startswith("💼 Потенциальный инвестор\nИмя: Juan Pérez")
    assert "под инвестиции" in investor and "сегодня" in investor
    single = person_card(_person(Sighting(POST, "x" * 500, "buyer", NOW - timedelta(days=1), None, {}), name=None),
                         location=None, now=NOW)
    assert "Имя:" not in single and "…»" in single and "вчера" in single
    assert "упомянув объект из комментария" in single and "1. Объект — «" in single


def test_contacts_are_phone_numbers_and_emails_not_prices() -> None:
    assert contacts_in("precio 50.000 € y 2000 m2") == []
    assert contacts_in("WhatsApp 612345678 / 612345678") == ["612345678"]
    assert contacts_in("juan@example.com") == ["juan@example.com"]


def test_only_the_objects_facts_are_kept_never_the_sellers_details() -> None:
    payload = {**LAND_FACTS, "location": "Madrid", "who": "Agencia X", "summary_ru": "…", "price_signals": ["x"]}
    assert object_facts(payload) == {**LAND_FACTS, "location": "Madrid"}
    assert object_facts(None) == {}


# --- the worker -------------------------------------------------------------------------------------


class FakeBrowser:
    def __init__(self, pages: dict[str, dict] | None = None) -> None:
        self.pages = pages or {}
        self.visits: list[str] = []
        self.released: list[str] = []
        self.down = False

    async def acquire(self, profile_id, profile_name, persisted_state, *, platform="facebook") -> BrowserLease:
        if self.down:
            raise ConnectionError("browser down")
        return BrowserLease(profile_id, "token")

    async def snapshot(self, lease, url, timeout_ms):
        self.visits.append(url)
        page = self.pages.get(url)
        if isinstance(page, Exception):
            raise page
        return page or {"url": url, "title": "Facebook", "text": "", "comments": []}

    async def release(self, lease, next_state="READY") -> None:
        self.released.append(next_state)


class FakeJudge:
    model = "openai/gpt-4o-mini"

    def __init__(self, fail: bool = False) -> None:
        self.fail, self.calls = fail, 0

    async def judge(self, context, comments):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return [Verdict("investor", 0.9, "Ищет объекты для вложений") if "inver" in c.text.lower()
                else Verdict("other", 0.9) for c in comments]


def _read(n: int, campaign: str = "c1") -> CommentRead:
    return CommentRead(campaign, f"f{n}", f"https://www.facebook.com/groups/1/posts/{n}/", 0, LAND)


def _page(url: str, *texts: str) -> dict:
    return {"url": url, "title": "Facebook", "text": "", "comments": [
        {"author": f"P{i}", "author_url": f"https://www.facebook.com/groups/1/user/1000000000{i}/", "text": text}
        for i, text in enumerate(texts)]}


def worker(store, browser, judge=None, **config):
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    return CommentLeadWorker(store, browser, judge, config=CommentConfig(**config), sleep=sleep), pauses


async def test_a_round_reads_a_few_posts_keeps_the_leads_and_hands_the_profile_back() -> None:
    reads = [_read(n) for n in range(1, 5)]
    store = MemoryLeadStore(reads)
    browser = FakeBrowser({reads[0].post_url: _page(reads[0].post_url, "Soy inversor", "@Ana mira"),
                           reads[1].post_url: _page(reads[1].post_url, "Busco inversión, escribidme")})
    lead_worker, pauses = worker(store, browser, FakeJudge(), reads_per_round=3)

    assert await lead_worker.step() == 3
    assert browser.visits == [r.post_url for r in reads[:3]]
    assert len(pauses) == 2 and all(6 <= p <= 14 for p in pauses)
    assert {k: v[:3] for k, v in store.finished.items()} == {
        reads[0].post_url: ("done", 2, 1), reads[1].post_url: ("done", 1, 1), reads[2].post_url: ("done", 0, 0)}
    assert [lead.comment.text for lead in store.leads.values()] == ["Soy inversor", "Busco inversión, escribidme"]
    assert {lead.judged_by for lead in store.leads.values()} == {"openai/gpt-4o-mini"}
    assert store.profile_state == "ready" and browser.released == ["READY"]
    assert [r.post_url for r in store.queued] == [reads[3].post_url]


async def test_the_rules_decide_when_the_model_fails() -> None:
    read = _read(1)
    store = MemoryLeadStore([read])
    judge = FakeJudge(fail=True)
    lead_worker, _ = worker(store, FakeBrowser({read.post_url: _page(read.post_url, "Me interesa, ¿precio?")}), judge)
    await lead_worker.step()
    assert judge.calls == 1
    assert [(lead.verdict.role, lead.judged_by) for lead in store.leads.values()] == [("buyer", "rules")]


async def test_nothing_is_read_while_facebook_is_busy_limited_or_breaker_open() -> None:
    for store in (MemoryLeadStore([_read(1)], busy=True), MemoryLeadStore([_read(1)], profile_state="in_use"),
                  MemoryLeadStore([_read(1)], breaker="safety breaker open"), MemoryLeadStore([_read(1)], today=40)):
        browser = FakeBrowser()
        lead_worker, _ = worker(store, browser)
        assert await lead_worker.step() == 0
        assert browser.visits == [] and len(store.queued) == 1


async def test_the_daily_cap_bounds_a_round() -> None:
    store = MemoryLeadStore([_read(n) for n in range(1, 5)], today=39)
    browser = FakeBrowser()
    lead_worker, _ = worker(store, browser, reads_per_round=3)
    assert await lead_worker.step() == 1
    assert len(browser.visits) == 1


async def test_a_challenge_stops_at_once_for_a_human_and_the_rest_waits() -> None:
    reads = [_read(n) for n in range(1, 4)]
    store = MemoryLeadStore(reads)
    browser = FakeBrowser({reads[1].post_url: {"url": "https://www.facebook.com/checkpoint/1501092823525282/",
                                               "title": "Facebook", "text": ""}})
    lead_worker, _ = worker(store, browser, reads_per_round=3)
    assert await lead_worker.step() == 1
    assert browser.visits == [reads[0].post_url, reads[1].post_url], "nothing is opened after a challenge"
    state, _, _, error = store.finished[reads[1].post_url]
    assert state == "failed" and error.startswith("facebook_challenge:")
    assert [r.post_url for r in store.queued] == [reads[2].post_url]
    assert store.profile_state == "human_verification_required"
    assert browser.released == ["VERIFICATION_REQUIRED"]


async def test_an_unreadable_post_is_retried_then_given_up() -> None:
    read = _read(1)
    store = MemoryLeadStore([read])
    browser = FakeBrowser({read.post_url: TimeoutError()})
    lead_worker, _ = worker(store, browser, max_attempts=2)
    await lead_worker.step()
    assert [r.post_url for r in store.queued] == [read.post_url] and store.queued[0].attempts == 1
    await lead_worker.step()
    assert store.finished[read.post_url][0] == "failed" and not store.queued
    assert store.profile_state == "ready"


async def test_a_browser_outage_puts_the_posts_back() -> None:
    store = MemoryLeadStore([_read(1)])
    browser = FakeBrowser()
    browser.down = True
    lead_worker, _ = worker(store, browser)
    assert await lead_worker.step() == 0
    assert len(store.queued) == 1 and store.profile_state == "ready"


def test_unsafe_reader_settings_are_refused() -> None:
    with pytest.raises(ValueError):
        CommentConfig(reads_per_round=0)
    with pytest.raises(ValueError):
        CommentConfig(pause_min_seconds=10, pause_max_seconds=5)


# --- the runner -------------------------------------------------------------------------------------


GOAL = "Найди землю на продажу в Мадриде до 60 000 €"
INVESTORS = "Найди инвесторов в Мадриде"
OBJECT = {"relevant": True, "listing_kind": "offer", "deal_type": "sale", "property_type": "land",
          "price_amount": 50_000, "price_currency": "EUR", "location": "Madrid", "country": "ES"}


async def _runner(mode: str = "all", *, goal: str = GOAL, **config):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    clock = Clock()
    runner = CampaignRunner(campaigns, store, messenger, None, now=clock, owner_ids={7},
                            config=RunnerConfig(relevance_fail_closed=False, comment_leads=mode, **config))
    cid = await campaigns.create(plan_campaign(goal), chat_id=CHAT, requested_by=8, source_text=goal, actor="telegram:8")
    await campaigns.set_state(cid, "running", "campaign:test")
    return campaigns, store, messenger, clock, runner, cid


async def test_a_sent_facebook_post_is_queued_once_and_other_links_never() -> None:
    _, store, _, _, runner, cid = await _runner(comment_max_posts=2)
    store.add_finding(cid, "f1", "🏠 one", payload=OBJECT, url=POST)
    store.add_finding(cid, "f2", "🏠 site", payload=OBJECT, url="https://www.idealista.com/inmueble/1/")
    store.add_finding(cid, "f3", "🏠 two", payload=OBJECT, url="https://www.facebook.com/groups/9/permalink/10/")
    store.add_finding(cid, "f4", "🏠 three", payload=OBJECT, url="https://www.facebook.com/groups/9/posts/11/")
    await runner.step(cid)
    assert store.comment_reads[cid] == {"f1": POST, "f3": "https://www.facebook.com/groups/9/permalink/10/"}


@pytest.mark.parametrize(("mode", "goal", "queued"), [
    ("off", GOAL, False),
    ("investors", GOAL, False),
    ("investors", INVESTORS, True),
    ("all", GOAL, True),
])
async def test_the_mode_decides_which_campaigns_read_comments(mode: str, goal: str, queued: bool) -> None:
    _, store, _, _, runner, cid = await _runner(mode, goal=goal)
    store.add_finding(cid, "f1", "🏠 one", payload=OBJECT, url=POST)
    await runner.step(cid)
    assert bool(store.comment_reads.get(cid)) is queued


def _stored(n: int) -> Person:
    return Person(f"fb:{n}", f"https://www.facebook.com/profile.php?id={n}", f"P{n}",
                  (Sighting(POST, "Me interesa", "buyer", datetime(2026, 9, 20, tzinfo=UTC), None, LAND_FACTS),))


async def test_a_property_search_never_sends_the_stored_people_and_ends_without_waiting() -> None:
    campaigns, store, messenger, _, runner, cid = await _runner()
    store.people["Madrid"] = [_stored(1)]
    store.add_finding(cid, "f1", "🏠 one", payload=OBJECT, url=POST)
    await runner.step(cid)
    assert not any("Хочет купить" in t for _, _, t in messenger.sent)
    assert (await campaigns.get(cid)).state == "completed", "the comments are read later, the search does not wait"


async def test_an_investor_search_sends_each_stored_person_of_its_city_once() -> None:
    _, store, messenger, _, runner, cid = await _runner(goal=INVESTORS, max_people=2)
    store.people["Madrid"] = [_stored(1), _stored(2), _stored(3)]
    store.people["Barcelona"] = [_stored(9)]
    messenger.fail = 1  # Telegram down: the person is offered again next step
    await runner.step(cid)
    await runner.step(cid)
    await runner.step(cid)
    cards = [t for _, _, t in messenger.sent if t.startswith("🏠 Хочет купить")]
    assert [c.splitlines()[2] for c in cards] == [
        "Профиль: https://www.facebook.com/profile.php?id=1", "Профиль: https://www.facebook.com/profile.php?id=2"]
    assert cards[0] == person_card(_stored(1), location="Madrid", now=runner.now())
    assert set(store.deliveries) == {(cid, "fb:1"), (cid, "fb:2")}, "at most max_people, only this city"
    assert not messenger.sent[-1][2].startswith("🏠"), "the status is the last message again"


# --- the browser's comment extraction (real Chromium, when installed) ------------------------------

FACEBOOK_POST_PAGE = """<html><body><a href="https://www.facebook.com/me.owner">Tu perfil</a>
<div role="article" aria-label="Post">
  <a href="https://www.facebook.com/groups/123/user/100000000000009/">Vendedor</a>
  <div dir="auto">Vendo parcela 2000 m2 en Madrid, 50.000 €</div>
  <a href="https://www.facebook.com/groups/123/posts/456/">3 h</a>
  <div role="article" aria-label="Comment by Juan Pérez 2 hours ago">
    <a href="https://www.facebook.com/groups/123/user/100012345678/?__cft__[0]=x"><svg></svg></a>
    <a href="https://www.facebook.com/groups/123/user/100012345678/?__cft__[0]=x"><span>Juan Pérez</span></a>
    <div dir="auto">Me interesa, ¿precio final?</div>
    <a href="https://www.facebook.com/groups/123/posts/456/?comment_id=789">2 h</a>
    <div role="article" aria-label="Reply by Vendedor 1 hour ago">
      <a href="https://www.facebook.com/groups/123/user/100000000000009/"><span>Vendedor</span></a>
      <div dir="auto">Te escribo por privado</div>
    </div>
  </div>
  <div role="article" aria-label="Comentario de Olga">
    <a href="https://www.facebook.com/olga.invest?__tn__=R"><span>Olga Invest</span></a>
    <div dir="auto"><span dir="auto">Инвестирую в землю, напишите в лс</span></div>
  </div>
</div></body></html>"""


async def test_the_browser_script_reads_each_comment_and_the_posts_author() -> None:
    from pathlib import Path

    playwright = pytest.importorskip("playwright.async_api")
    chromium = Path("/opt/pw-browsers/chromium")
    if not chromium.exists():
        pytest.skip("Chromium is not installed here")
    from bot.browser_session.manager import _FACEBOOK_COMMENTS_JS

    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch(executable_path=str(chromium))
        try:
            page = await browser.new_page()
            await page.set_content(FACEBOOK_POST_PAGE)
            raw = await page.evaluate(_FACEBOOK_COMMENTS_JS)
        finally:
            await browser.close()
    assert [(c["author"], c["text"]) for c in raw["items"]] == [
        ("Juan Pérez", "Me interesa, ¿precio final?"), ("Vendedor", "Te escribo por privado"),
        ("Olga Invest", "Инвестирую в землю, напишите в лс")]
    assert raw["items"][0]["comment_url"] == "https://www.facebook.com/groups/123/posts/456/?comment_id=789"
    comments = parse_comments(raw["items"], post_author=raw["post_author_url"])
    assert [(c.author, c.profile_url) for c in comments] == [
        ("Juan Pérez", "https://www.facebook.com/profile.php?id=100012345678"),
        ("Olga Invest", "https://www.facebook.com/olga.invest")]
