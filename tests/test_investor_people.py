"""Investor mode (stage 5): spec-driven queries and judging, enrichment, scoring, one card per person, groups."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.people import (
    GROUPS,
    amounts_in,
    build_cards,
    enrich,
    enrichable_url,
    extract,
    investor_of,
    person_key,
    score,
)
from bot.campaign.reach import (
    Candidate,
    Contact,
    Judged,
    MemoryReachStore,
    OpenRouterReachJudge,
    PostgresReachStore,
    ReachCampaign,
    ReachConfig,
    ReachWorker,
    contact_card,
    plan_queries,
)
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.web_search.searxng import SearchHit
from tests.test_campaign_runner import CHAT, Clock, FakeMessenger
from tests.test_investor_reach import LINKEDIN_Q, FakeSearcher
from tests.test_near_match import needs_db
from tests.test_near_match import pool as pool

NOW = datetime(2026, 10, 1, tzinfo=UTC)
SPEC = {"investor": {"who": ["fund", "family_office"], "ticket": {"min": 500_000, "max": 2_000_000, "currency": "EUR"},
                     "asset_class": ["residential"], "geography": ["Madrid"], "languages": ["es", "en"],
                     "user_role": "raising"}}
INVESTOR = investor_of(SPEC)
MADRID = ReachCampaign("c1", "Madrid", {"es": "Madrid", "en": "Madrid", "ru": "Мадрид"}, ("es", "en", "ru"),
                       "Фонды в Мадриде", "Найди фонды", "ES", INVESTOR)


def _contact(n: int, kind: str = "fund", name: str | None = None, **kw) -> Contact:
    return Contact(f"{n:064d}", f"https://example.com/p{n}", kw.pop("platform", "web"), kind, name or f"Person Number{n}",
                   **kw)


# --- the spec ---------------------------------------------------------------------------------------------


def test_the_investor_block_is_read_from_the_spec_only_when_filled() -> None:
    assert INVESTOR["who"] == ["fund", "family_office"] and INVESTOR["ticket"]["max"] == 2_000_000
    assert investor_of(None) is None and investor_of({}) is None and investor_of({"investor": {"who": []}}) is None
    assert investor_of({"investor": "junk"}) is None


def test_spec_queries_name_who_and_asset_class_first_then_the_generic_templates() -> None:
    texts = [q.text for q in plan_queries(MADRID)]
    assert any(t == "fondo de inversión inmobiliaria residential Madrid" for t in texts)
    assert any("family office" in t.casefold() and "Madrid" in t for t in texts[:8])
    assert texts[-1] != texts[0] and LINKEDIN_Q in texts, "the generic templates still follow"
    assert len(set(texts)) == len(texts)
    # without a spec: exactly today's queries
    plain = ReachCampaign("c", "Madrid", MADRID.aliases, MADRID.languages)
    assert next(q.text for q in plan_queries(plain)) == LINKEDIN_Q
    # a place outside Spain: no Spanish spec query
    bali = ReachCampaign("c", "Ubud", {"en": "Ubud"}, ("es", "en"), country="ID", investor=INVESTOR)
    assert all(q.language != "es" for q in plan_queries(bali))


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def complete(self, model, system, user, **kw) -> str:
        self.calls.append((model, system, user))
        if kw.get("name") == "reach_queries":
            return json.dumps({"queries": []})
        return json.dumps({"results": [{"index": 0, "kind": "fund", "relevant": True, "confidence": 0.9, "name": "F",
                                        "summary_ru": "Фонд"}]})


async def test_the_judge_and_query_prompts_carry_the_investor_block_as_hard_criteria() -> None:
    judge = OpenRouterReachJudge.__new__(OpenRouterReachJudge)
    judge.model, judge._client = "m", _Client()
    await judge.judge(MADRID, [Candidate("https://a.es/", "web", "page", "A", "fund")])
    await judge.queries(MADRID, 3)
    (_, system, user), (_, qsystem, quser) = judge._client.calls
    assert "HARD CRITERIA" in system and "contradicts" not in user
    data = json.loads(user.split("\n", 1)[1])
    assert data["investor"]["who"] == ["fund", "family_office"] and data["investor"]["ticket"]["max"] == 2_000_000
    assert data["investor"]["geography"] == ["Madrid"]
    assert "data.investor" in qsystem and json.loads(quser.split("\n", 1)[1])["investor"]["user_role"] == "raising"
    # no spec: today's prompts
    plain = ReachCampaign("c", "Madrid", {}, ("en",))
    judge._client.calls.clear()
    await judge.judge(plain, [Candidate("https://a.es/", "web", "page", "A", "x")])
    assert "HARD CRITERIA" not in judge._client.calls[0][1] and "investor" not in json.loads(
        judge._client.calls[0][2].split("\n", 1)[1])


# --- enrichment ---------------------------------------------------------------------------------------------

SITE = """<html><head><title>Inversiones | Capital Norte SL</title>
<meta name="description" content="Fondo"><meta property="article:modified_time" content="x">
<script type="application/ld+json">{"dateModified": "2026-08-15"}</script></head>
<body><h1>Fondo inmobiliario en Madrid</h1><p>Invertimos en vivienda residencial, tickets de 1 000 000 EUR.</p>
<p>Llámenos: +34 612 345 678</p><a href="mailto:Info@CapitalNorte.es">escríbenos</a>
<img src="logo@2x.png"></body></html>"""
CHANNEL = """<html><head><title>Madrid Investors</title></head><body>
<div class="tgme_widget_message_text">Ищем проекты. Контакт: @manager</div>
<a href="https://madridinvestors.example/">site</a>
<time datetime="2026-09-20T10:00:00+00:00">Sep 20</time><time datetime="2026-01-02T10:00:00+00:00">Jan 2</time>
</body></html>"""


class FakeFetcher:
    def __init__(self, pages: dict[str, str] | None = None, robots: bool = True) -> None:
        self.pages, self.robots, self.fetched = pages or {}, robots, []

    async def allowed(self, url: str) -> bool:
        return self.robots

    async def fetch(self, url: str, *, country: str | None = None):
        from types import SimpleNamespace

        self.fetched.append(url)
        if url not in self.pages:
            raise RuntimeError("404")
        return SimpleNamespace(url=url, html=self.pages[url])


@pytest.mark.parametrize(("platform", "url", "expected"), [
    ("linkedin", "https://es.linkedin.com/in/juan", None),
    ("instagram", "https://www.instagram.com/x/", None),
    ("tiktok", "https://www.tiktok.com/@x", None),
    ("x", "https://x.com/juan", None),
    ("reddit", "https://www.reddit.com/user/x", None),
    ("reddit", "https://old.reddit.com/user/x", None),
    ("web", "https://old.reddit.com/user/x", None),
    ("web", "https://www.linkedin.com/company/x", None),
    ("telegram", "https://t.me/madridinv", "https://t.me/s/madridinv"),
    ("telegram", "https://t.me/s/madridinv", "https://t.me/s/madridinv"),
    ("telegram", "https://t.me/madridinv/42", "https://t.me/s/madridinv"),
    ("telegram", "https://t.me/+secretinvite", None),
    ("web", "https://www.capitalnorte.es/", "https://www.capitalnorte.es/"),
])
def test_only_sites_and_public_channels_are_opened(platform: str, url: str, expected) -> None:
    assert enrichable_url(platform, url) == expected


def test_a_site_gives_emails_phone_website_company_description_and_last_activity() -> None:
    e = extract(SITE, "https://www.capitalnorte.es/", "web")
    assert e.emails == ("info@capitalnorte.es",), "the image name is not an e-mail"
    assert e.phones == ("+34 612 345 678",)
    assert e.website == "https://capitalnorte.es" and e.company == "Capital Norte SL"
    assert e.description.startswith("Fondo inmobiliario en Madrid") and len(e.description) <= 600
    assert e.last_activity.isoformat() == "2026-08-15"
    assert e.contacts()["emails"] == ["info@capitalnorte.es"]
    c = extract(CHANNEL, "https://t.me/s/madridinv", "telegram")
    assert c.website == "https://madridinvestors.example/" and c.last_activity.isoformat() == "2026-09-20"


async def test_enrichment_respects_blocked_hosts_robots_and_failures() -> None:
    fetcher = FakeFetcher({"https://www.capitalnorte.es/": SITE})
    assert await enrich(fetcher, "linkedin", "https://es.linkedin.com/in/juan") is None and fetcher.fetched == []
    assert (await enrich(fetcher, "web", "https://www.capitalnorte.es/")).emails
    assert await enrich(fetcher, "web", "https://down.example/") is None, "a failed page is no enrichment"
    assert await enrich(FakeFetcher({"https://www.capitalnorte.es/": SITE}, robots=False), "web",
                        "https://www.capitalnorte.es/") is None


async def test_the_worker_enriches_relevant_results_once_up_to_the_cap_and_scores_them() -> None:
    class Judge:
        model = "m"

        async def judge(self, campaign, candidates):
            return [Judged("fund", True, 0.9, f"Fund{i}", "Фонд") for i, _ in enumerate(candidates)]

    hits = [SearchHit(f"https://site{i}.es/", f"Fondo {i} Madrid", "") for i in range(4)]
    hits.append(SearchHit("https://es.linkedin.com/in/juan", "Juan - Madrid", ""))
    fetcher = FakeFetcher({f"https://site{i}.es/": SITE for i in range(4)})
    store = MemoryReachStore([MADRID])
    worker = ReachWorker(store, FakeSearcher({LINKEDIN_Q: hits, **{q.text: hits for q in plan_queries(MADRID)[:1]}}),
                         Judge(), config=ReachConfig(queries_per_tick=1, enrich_per_campaign=2), fetcher=fetcher)
    await worker.tick()
    assert fetcher.fetched == ["https://site0.es/", "https://site1.es/"], "the cap, and LinkedIn is never opened"
    assert sorted(store.enrichments) == sorted(k for k, s in store.contacts.items()
                                               if s.candidate.url in fetcher.fetched)
    assert len(store.scores) == 5 and all(0 <= v <= 100 for v in store.scores.values())
    assert await store.enriched_count("c1") == 2
    # no fetcher (web stage off): results are kept and scored, nothing is opened
    off = MemoryReachStore([MADRID])
    await ReachWorker(off, FakeSearcher({plan_queries(MADRID)[0].text: hits}), Judge(),
                      config=ReachConfig(queries_per_tick=1)).tick()
    assert off.enrichments == {} and len(off.scores) == 5


# --- scoring and one card per person ----------------------------------------------------------------------


def test_scoring_adds_up_to_one_hundred_with_the_reasons() -> None:
    contact = _contact(1, "fund", title="Fondo inmobiliario Madrid", profile_text="Vivienda residencial, 1 000 000 EUR",
                       contacts={"emails": ["a@b.es"], "last_activity": "2026-08-15"})
    points, reasons = score(contact, INVESTOR, now=NOW)
    assert points == 100 and len(reasons) == 6
    assert "тип совпадает с запросом" in reasons and "тикет в вашем диапазоне" in reasons
    # a developer is not a fund: nothing is earned
    other = _contact(2, "developer", title="x")
    assert score(other, INVESTOR, now=NOW) == (0, [])
    only_kind = _contact(3, "fund", title="x")
    assert score(only_kind, INVESTOR, now=NOW)[0] == 40
    # a sum outside the ticket, a contact older than a year
    stale = _contact(4, "fund", profile_text="tickets 5 million", contacts={"phones": ["+34 612 345 678"],
                                                                         "last_activity": "2024-01-01"})
    assert score(stale, INVESTOR, now=NOW) == (50, ["тип совпадает с запросом", "есть контакт"])
    assert score(only_kind, None)[0] == 0, "without a spec only place, contact and activity count"


def test_amounts_need_a_currency_or_a_multiplier() -> None:
    assert amounts_in("tickets €500k-€2M, or 750 000 EUR, 3 млн") == [500_000.0, 2_000_000.0, 750_000.0, 3_000_000.0]
    assert amounts_in("call 612 345 678 in 2026 for 2 months") == []


def test_person_key_normalises_name_company_and_city() -> None:
    assert person_key("Juan Pérez", None, "Madrid") == person_key("  JUAN  perez ", "", "madrid")
    assert person_key("Juan Pérez", "Capital Norte SL", "Madrid") == person_key("juan perez", "capital norte", "Madrid")
    assert person_key("Juan Pérez", "Capital Norte", "Madrid") != person_key("Juan Pérez", "Otra", "Madrid")
    assert person_key("Ana", None, "Madrid") == "" and person_key(None, "X", "Madrid") == ""
    assert person_key("Ana", "Fondo Sur", "Madrid") != ""


def test_the_same_person_on_two_platforms_is_one_card_with_the_best_score_and_all_links() -> None:
    linkedin = _contact(1, "investor", "Juan Pérez", platform="linkedin", title="Inversor Madrid")
    site = _contact(2, "fund", "juan perez", title="Fondo residencial Madrid", profile_text="vivienda 1 000 000 EUR",
                    contacts={"emails": ["j@x.es"]})
    cards = build_cards([linkedin, site], INVESTOR, places=["Madrid"], now=NOW)
    assert len(cards) == 1
    card = cards[0]
    assert card.contact is site and card.score > score(linkedin, INVESTOR, places=["Madrid"])[0]
    assert card.keys == (site.delivery_key, linkedin.delivery_key)
    assert card.links == (("linkedin", linkedin.url),)
    text = contact_card(card.contact, score=card.score, reasons=card.reasons, also=card.links)
    assert f"Соответствие: {card.score}/100 — тип совпадает с запросом" in text
    assert "Также: LinkedIn https://example.com/p1" in text and "Контакт: j@x.es" in text


def test_groups_come_investors_then_developers_then_agents_each_by_score() -> None:
    contacts = [_contact(1, "agent", title="Madrid"), _contact(2, "developer", title="Madrid"),
                _contact(3, "fund", title="x"), _contact(4, "fund", title="Madrid"), _contact(5, "network"),
                _contact(6, "investor", title="Madrid"), _contact(7, "agency", title="Madrid", profile_text="Agencia", contacts={"emails": ["a@b.es"]})]
    cards = build_cards(contacts, {"who": ["fund", "developer", "agency", "network", "private", "agent"]},
                        places=["Madrid"], now=NOW)
    assert [(c.group, c.contact.name[-1]) for c in cards] == [
        ("investors", "4"), ("investors", "6"), ("investors", "3"),
        ("developers", "2"),
        ("agents", "7"), ("agents", "1"), ("agents", "5")]
    assert [h for _, h, _ in GROUPS][:3] == ["🏦 Инвесторы и фонды", "🏗 Девелоперы", "🤝 Агенты и сети"]


# --- the runner -------------------------------------------------------------------------------------------


async def test_an_investor_search_sends_group_headers_once_cards_by_score_and_merged_people_once() -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, now=Clock(),
                            config=RunnerConfig(relevance_fail_closed=False, max_people=10, max_stream_per_step=10))
    goal = "Найди инвесторов в Мадриде"
    cid = await campaigns.create(plan_campaign(goal), chat_id=CHAT, requested_by=8, source_text=goal, actor="t",
                                 spec=SPEC)
    await campaigns.set_state(cid, "running", "campaign:test")
    store.contacts["Madrid"] = [
        _contact(1, "agent", "Ana Gómez", title="Agente Madrid"),
        _contact(2, "fund", "Fondo Uno", title="Fondo Madrid", contacts={"emails": ["a@b.es"]}),
        _contact(3, "developer", "Dev Dos", title="Madrid"),
        _contact(4, "fund", "Fondo Cuatro", title="x"),
        _contact(5, "fund", "Juan Pérez", platform="linkedin", title="Madrid"),
        _contact(6, "fund", "juan perez", title="Madrid fondo", profile_text="Fondo", contacts={"emails": ["j@b.es"]}),
    ]
    await runner.step(cid)
    await runner.step(cid)
    texts = [t for _, _, t in messenger.sent]
    body = [t for t in texts if t.startswith(("🏦 Инвесторы и фонды", "🏗 Девелоперы", "🤝 Агенты и сети"))
            or "Соответствие" in t]
    assert [t.splitlines()[0] for t in body if "Соответствие" not in t] == [
        "🏦 Инвесторы и фонды", "🏗 Девелоперы", "🤝 Агенты и сети"], "one header per group"
    names = [next(line for line in t.splitlines() if line.startswith("Имя:")) for t in body if "Соответствие" in t]
    assert names[2:] == ["Имя: Fondo Cuatro", "Имя: Dev Dos", "Имя: Ana Gómez"] and set(names[:2]) == {
        "Имя: Fondo Uno", "Имя: juan perez"}
    assert len(names) == 5, "the two Juan Pérez contacts are one card"
    assert sum("Также:" in t for t in body) == 1
    before = len(messenger.sent)
    await runner.step(cid)
    assert len(messenger.sent) == before, "every contact, merged ones included, is sent once"


# --- PostgreSQL --------------------------------------------------------------------------------------------


@needs_db
async def test_postgres_037_columns_hold_the_enrichment_and_the_spec_reaches_the_worker(pool) -> None:
    from bot.campaign.reach import Stored, contact_extras
    from bot.campaign.store import PostgresCampaignStore

    campaigns = PostgresCampaignStore(pool)
    goal = "Найди инвесторов в Мадриде"
    cid = await campaigns.create(plan_campaign(goal), chat_id=CHAT, requested_by=8, source_text=goal, actor="t",
                                 spec=SPEC)
    await campaigns.set_state(cid, "running", "campaign:test")
    store = PostgresReachStore(pool)
    opened = await store.open_campaigns()
    assert opened[0].investor["who"] == ["fund", "family_office"]
    candidate = Candidate("https://www.capitalnorte.es/", "web", "page", "Capital Norte", "fondo Madrid")
    assert await store.save([Stored(candidate, Judged("fund", True, 0.9, "Capital Norte", "Фонд"), "Madrid", cid,
                                    "rules")]) == 1
    row = await pool.fetchrow("select enriched_at, contacts::text as c, profile_text, score from reach_contacts")
    assert row["enriched_at"] is None and json.loads(row["c"]) == {} and row["profile_text"] is None and row["score"] is None
    assert await store.enriched_count(cid) == 0
    await store.save_enrichment(candidate.url_key, extract(SITE, candidate.url, "web"), 87)
    assert await store.enriched_count(cid) == 1
    extras = (await contact_extras(pool, [candidate.url_key]))[candidate.url_key]
    assert extras["score"] == 87 and extras["contacts"]["emails"] == ["info@capitalnorte.es"]
    assert extras["profile_text"].startswith("Fondo inmobiliario") and extras["enriched_at"] is not None
    other = Candidate("https://other.es/", "web", "page", "O", "x")
    await store.save([Stored(other, Judged("fund", True, 0.9), "Madrid", cid, "rules")])
    await store.save_enrichment(other.url_key, None, 40)
    assert (await contact_extras(pool, [other.url_key]))[other.url_key]["score"] == 40
    assert await store.enriched_count(cid) == 1, "a score alone is not an enrichment"
    with pytest.raises(Exception, match="score"):
        await pool.execute("update reach_contacts set score = 101")


def test_settings_default_comment_leads_to_investors_and_pass_the_enrichment_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.campaign.settings import CampaignRunnerSettings

    monkeypatch.delenv("CAMPAIGN_COMMENT_LEADS", raising=False)
    monkeypatch.setenv("INVESTOR_REACH_ENRICH_PER_CAMPAIGN", "7")
    settings = CampaignRunnerSettings(_env_file=None, DATABASE_URL="postgresql://x", TELEGRAM_TOKEN="t")
    assert settings.comment_leads == "investors" and settings.reach_config().enrich_per_campaign == 7


# --- review fixes: telegram pages, phones, scoring ----------------------------------------------------------------------------

TME_PAGE = """<html><head><title>Ana Gómez – Telegram</title></head><body>
<div class="tgme_widget_message_text">Invertimos en vivienda. Llame al 150 000 - 300 000 o desde 2019 2020 2021</div>
</body></html>"""


def test_a_telegram_page_never_gives_the_platform_as_the_company() -> None:
    e = extract(TME_PAGE, "https://t.me/s/anagomez", "telegram")
    assert e.company is None and e.phones == ()
    assert extract("<title>Telegram</title>", "https://t.me/s/x", "telegram").company is None
    assert extract("<title>Madrid Investors | Telegram</title>", "https://t.me/s/x", "telegram").company is None
    assert extract("<title>Hola | Capital Norte</title>", "https://t.me/s/x", "telegram").company == "Capital Norte"
    # a one-word name is no identity just because the company is a platform brand
    assert person_key("Ana", "Telegram", "Madrid") == "" and person_key("Ana", "LinkedIn", "Madrid") == ""
    assert person_key("Ana Gómez", "Telegram", "Madrid") == person_key("Ana Gómez", None, "Madrid")


@pytest.mark.parametrize("url", ["https://t.me/joinchat/AAAA", "https://t.me/addstickers/pack", "https://t.me/share/url",
                                 "https://t.me/+invitehash", "https://t.me/s/joinchat", "https://t.me/s/+hash"])
def test_telegram_invite_and_action_links_are_never_opened(url: str) -> None:
    assert enrichable_url("telegram", url) is None


def test_page_text_phones_need_a_plus_or_a_tel_link_and_are_not_ranges_or_years() -> None:
    def phones(body: str) -> tuple[str, ...]:
        return extract(f"<html><head><title>x</title></head><body><p>{body}</p></body></html>",
                       "https://a.example/", "web").phones

    assert phones("Precio 150 000 - 300 000 euros") == ()
    assert phones("Años 2019 2020 2021") == ()
    assert phones("Llame 612 345 678") == (), "no leading + and no tel: link"
    assert phones("Llame +34 612 345 678") == ("+34 612 345 678",)
    assert phones("rango +34 612 - 345 678") == ()
    tel = extract('<a href="tel:+34612345678">x</a>', "https://a.example/", "web")
    assert tel.phones == ("+34612345678",)


def test_amounts_ignore_areas_and_distances() -> None:
    assert amounts_in("terreno 500 m2, 1200 m², 3000 м², a 5 km, 2 km de Madrid") == []
    assert amounts_in("€5 000 m²") == []
    assert amounts_in("tickets €500k and 120 m2") == [500_000.0]


def test_a_company_is_not_a_kind_match_a_snippet_is_capped_and_asset_words_are_whole_words() -> None:
    fund = {"who": ["fund"], "asset_class": ["land", "commercial"], "geography": ["Madrid"]}
    # kind +40 only for a real match: a company earns none when who is stated, +10 when it is not
    assert score(_contact(1, "company", title="x"), {"who": ["fund"]}, now=NOW)[0] == 0
    assert score(_contact(1, "company", title="x"), {"who": []}, now=NOW) == (10, ["компания"])
    assert score(_contact(1, "investor", title="x"), {"who": []}, now=NOW)[0] == 0
    assert score(_contact(1, "fund", title="x"), {"who": ["fund"]}, now=NOW)[0] == 40
    # the asset words need word boundaries
    for text in ("Finland landing page", "Family office in Finland", "семейный офис"):
        assert "класс актива" not in " ".join(score(_contact(2, "fund", title=text, profile_text="x"), fund, now=NOW)[1])
    for text in ("Land for sale", "Terrenos y lands", "Oficina comercial"):
        assert any("класс актива" in r for r in score(_contact(2, "fund", title=text, profile_text="x"), fund, now=NOW)[1])
    office = {"asset_class": ["commercial"]}
    assert not any("актива" in r for r in score(_contact(3, "fund", title="Family Office Madrid", profile_text="x"),
                                                office, now=NOW)[1])
    assert any("актива" in r for r in score(_contact(3, "fund", title="Office space", profile_text="x"), office,
                                            now=NOW)[1])
    # snippet-only evidence (no page text) never goes above 60
    rich = {"who": ["fund"], "asset_class": ["residential"], "geography": ["Madrid"],
            "ticket": {"min": 500_000, "max": 2_000_000}}
    snippet = _contact(4, "fund", title="Fondo Madrid vivienda 1 000 000 EUR", contacts={"emails": ["a@b.es"]})
    assert score(snippet, rich, now=NOW)[0] == 60
    read = _contact(4, "fund", title="Fondo Madrid vivienda 1 000 000 EUR", profile_text="Fondo",
                    contacts={"emails": ["a@b.es"]})
    assert score(read, rich, now=NOW)[0] == 100


async def test_the_enrichment_cap_counts_attempts_and_pages_are_read_three_at_a_time() -> None:
    import asyncio
    from types import SimpleNamespace

    class Slow:
        def __init__(self) -> None:
            self.running = self.peak = 0
            self.fetched: list[str] = []

        async def allowed(self, url: str) -> bool:
            return True

        async def fetch(self, url: str, *, country: str | None = None):
            self.fetched.append(url)
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(0.01)
            self.running -= 1
            if "bad" in url:
                raise RuntimeError("503")
            return SimpleNamespace(url=url, html=SITE)

    class Judge:
        model = "m"

        async def judge(self, campaign, candidates):
            return [Judged("fund", True, 0.9, f"Fund{i}", "Фонд") for i, _ in enumerate(candidates)]

    hits = [SearchHit(f"https://{'bad' if i < 2 else 'ok'}{i}.es/", f"Fondo {i} Madrid", "") for i in range(8)]
    fetcher = Slow()
    store = MemoryReachStore([MADRID])
    worker = ReachWorker(store, FakeSearcher({q.text: hits for q in plan_queries(MADRID)[:1]}), Judge(),
                         config=ReachConfig(queries_per_tick=1, enrich_per_campaign=5), fetcher=fetcher)
    await worker.tick()
    assert len(fetcher.fetched) == 5, "failed fetches use cap slots too"
    assert fetcher.peak == 3, "at most three pages are read at once"
    assert await store.enriched_count("c1") == 5
    failed = [e for k, e in store.enrichments.items() if store.contacts[k].candidate.url.startswith("https://bad")]
    assert len(failed) == 2 and all(e.contacts() == {} and e.description == "" for e in failed)
    # the next tick has no room left: nothing more is opened
    await worker.tick()
    assert len(fetcher.fetched) == 5
