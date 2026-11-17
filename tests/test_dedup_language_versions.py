"""Q77 (``DEDUP_LANGUAGE_VERSIONS``): one Engel & Völkers ad in two languages (``/be/en/exposes/<uuid>`` and
``/be/fr/exposes/<uuid>``) came as two cards in the bench run b7-g9-s8. The shape of that run; mocks only."""

from __future__ import annotations

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import listing_of, same_object
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from bot.web_search.urls import language_versions
from tests.test_campaign_runner import FakeMessenger

EV = "https://www.engelvoelkers.com/be/{}/exposes/969d75e5-080e-5082-afe3-2c32d49621c7"
OTHER_EV = "https://www.engelvoelkers.com/be/fr/exposes/169d75e5-080e-5082-afe3-2c32d49621c7"
IDEALISTA = "https://www.idealista.com/inmueble/123456/"
FOTOCASA = "https://www.fotocasa.es/es/comprar/terreno/galapagar/123456/d"
BENCH = {"without_address": True, "district_not_street": True, "cross_portal_facts": True, "same_host_copy": True,
         "description_normalize": True, "same_lot": True, "twin_listings": True, "rent_platform_copy": True}
ON = {**BENCH, "language_versions": True}


def plot(url: str, **extra) -> dict:
    return {"price_amount": None, "area_m2": None, "plot_m2": 25800, "deal_type": "sale", "property_type": "land",
            "location": "Colada Charco de la Hoya, Galapagar, Comunidad de Madrid", "address": None,
            "advertiser": "Engel & Völkers", "original_post_link": url, **extra}


def same(a: dict, b: dict, **flags) -> bool:
    la = listing_of(a, url=a["original_post_link"])
    lb = listing_of(b, url=b["original_post_link"])
    assert same_object(la, lb, **flags) == same_object(lb, la, **flags)
    return same_object(la, lb, **flags)


def test_flag_off_by_default_and_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_LANGUAGE_VERSIONS", raising=False)
    assert RunnerConfig().dedup_language_versions is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_language_versions is False
    monkeypatch.setenv("DEDUP_LANGUAGE_VERSIONS", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_language_versions is True


def test_urls_of_one_ad_in_two_languages() -> None:
    assert language_versions(EV.format("en"), EV.format("fr"))
    assert language_versions(EV.format("en"), EV.format("fr").replace("www.", ""))
    assert language_versions("https://a.es/es-es/piso-centro/", "https://a.es/en-gb/piso-centro?utm=x")
    assert language_versions("https://a.es/piso-centro", "https://www.a.es/de/piso-centro")
    assert language_versions("https://www.idealista.com/inmueble/123456/", "https://www.idealista.com/en/inmueble/123456/")


def test_other_ads_are_not_language_versions() -> None:
    assert not language_versions(EV.format("en"), OTHER_EV)  # another UUID
    assert not language_versions(IDEALISTA, FOTOCASA)  # one number on two hosts
    assert not language_versions("https://a.es/piso?id=1", "https://a.es/piso?id=2")  # nothing dropped
    assert not language_versions("https://a.es/en/piso?id=1234567", "https://a.es/fr/piso?id=7654321")
    assert not language_versions("https://a.es/en/", "https://a.es/fr/")  # two home pages
    assert not language_versions("https://a.es/en/villa-mar", "https://a.es/fr/villa-sol")
    assert not language_versions(EV.format("en"), None)


def test_the_bench_pair_is_one_object_with_the_flag() -> None:
    en, fr = plot(EV.format("en")), plot(EV.format("fr"))
    assert not same(en, fr, **BENCH)  # b7-g9-s8: two cards
    assert same(en, fr, **ON)
    assert not same(en, plot(OTHER_EV), **ON)
    assert not same(plot(IDEALISTA), plot(FOTOCASA), **ON)
    big, small = plot(EV.format("en"), area_m2=25800), plot(EV.format("fr"), area_m2=12000)
    assert not same(big, small, **ON)  # a conflicting area still wins
    assert not same(en, plot(EV.format("fr"), deal_type="rent"), **ON)


# -- the runner --

GOAL = "Снять квартиру в Барселоне до 1500 € в месяц, 2 комнаты"


def flat(url: str) -> dict:
    return {"price_amount": 1100.0, "price_currency": "EUR", "area_m2": 70.0, "rooms": 2, "floor": None,
            "deal_type": "rent", "property_type": "apartment", "district": "Eixample",
            "location": "Eixample, Barcelona", "address": None, "original_post_link": url,
            "summary": "Piso en alquiler en Eixample, Barcelona."}


async def run(monkeypatch, **config) -> tuple[MemoryRunStore, FakeMessenger]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, dedup_held_findings=True, **config))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, lang in (("en", "en"), ("fr", "fr")):
        url = EV.format(lang)
        store.add_finding(cid, fid, f"🏠 {fid}", payload=flat(url), original=f"Ссылка: {url}",
                          vertical="real_estate", url=url)
    await runner.step(cid)
    return store, messenger


async def test_runner_off_two_cards(monkeypatch) -> None:
    store, messenger = await run(monkeypatch)
    assert len(messenger.findings()) == 2 and store.duplicates == {}


async def test_runner_on_one_card(monkeypatch) -> None:
    store, messenger = await run(monkeypatch, dedup_language_versions=True)
    assert len(messenger.findings()) == 1 and store.duplicates == {"fr": "en"}
