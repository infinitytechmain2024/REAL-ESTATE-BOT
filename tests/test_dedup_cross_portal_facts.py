"""Q32: Fotocasa and Habitaclia list one house with the districts named differently in the «address», so the
addresses share no street token and the facts rule was never reached (the shape of the bench's Marbella pairs; prices,
areas, rooms, districts and features as on the bench, invented URLs, agencies and a fictitious phone; mocks only)."""

from __future__ import annotations

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import listing_of, same_object
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_runner import FakeMessenger

PHONE = "+34600000000"  # fictitious
FOTO = "https://www.fotocasa.es/es/comprar/vivienda/marbella/piscina/{}/d"
HAB = "https://www.habitaclia.com/i{}.htm"
FEATURES_1 = ["piscina", "jardin", "terraza", "garaje", "ascensor"]


def house(district: str, *, price: float, area: float, rooms: int | None, features: list[str], address: str | None = None,
          location: str | None = None, advertiser: str = "Agencia Uno", **extra) -> dict:
    return {"price_amount": price, "price_currency": "EUR", "area_m2": area, "rooms": rooms, "deal_type": "sale",
            "property_type": "house", "district": district, "location": location or f"{district}, Marbella",
            "address": address or f"{district}, Marbella, Málaga", "advertiser": advertiser, "features": features,
            **extra}


# (Fotocasa payload, Habitaclia payload): cards 1/12, 2/13, 4/14, 6/15, 7/16, 9/17, 11/18 of the bench run.
PAIRS = [
    (house("Las Lomas de Río Verde", price=9_800_000, area=1200, rooms=5, features=FEATURES_1,
           advertiser="Agencia Uno Properties Slu", phone=PHONE),
     house("Milla de Oro", price=9_800_000, area=1200, rooms=5, features=FEATURES_1, advertiser="AGENCIA UNO REAL ESTATE")),
    (house("Valdeolletas - Las Cancelas - Xarblanca", price=890_000, area=224, rooms=3,
           features=["garaje", "piscina", "reformado", "trastero"], advertiser="Persona Ejemplo", phone=PHONE),
     house("Valdeolletas", price=890_000, area=224, rooms=3, features=["garaje", "piscina", "reformado", "trastero"],
           address="Marbella Centro, Marbella, Málaga", advertiser="INMOBILIARIA EJEMPLO")),
    (house("La Carolina - Guadalpín", price=4_500_000, area=650, rooms=5,
           features=["aire acondicionado", "ascensor", "garaje", "jardin", "piscina", "terraza", "trastero"],
           location="La Carolina - Guadalpín, Marbella Club, Marbella", advertiser="AGENCIA DOS", phone=PHONE),
     house("Milla de Oro", price=4_500_000, area=650, rooms=5,
           features=["aire acondicionado", "ascensor", "garaje", "jardin", "piscina", "terraza", "trastero"],
           location="Marbella Club, Milla de Oro, Marbella", advertiser="AGENCIA DOS")),
    (house("Sierra Blanca", price=19_995_000, area=1812, rooms=18,
           features=["ascensor", "garaje", "jardin", "piscina", "terraza", "trastero"], advertiser="Agencia Tres Sl",
           phone=PHONE),
     house("Milla de Oro", price=19_995_000, area=1812, rooms=18,
           features=["ascensor", "garaje", "jardin", "piscina", "terraza", "trastero"], advertiser="Agencia Tres Real")),
    (house("Sierra Blanca", price=2_450_000, area=564, rooms=4,
           features=["garaje", "jardin", "piscina", "reformado", "terraza"],
           location="Rocío de Nagüeles, Sierra Blanca, Marbella", advertiser="Agencia Tres Sl", phone=PHONE),
     house("Milla de Oro", price=2_450_000, area=564, rooms=4,
           features=["garaje", "jardin", "piscina", "reformado", "terraza"],
           location="Rocío de Nagüeles, Milla de Oro, Marbella", advertiser="Agencia Tres Real")),
    (house("Nueva Andalucía centro", price=2_195_000, area=567, rooms=4,
           features=["garaje", "piscina", "terraza", "trastero"], advertiser="Agencia Tres Sl", phone=PHONE),
     house("Nueva Andalucía", price=2_195_000, area=567, rooms=4, features=["garaje", "piscina", "terraza", "trastero"],
           advertiser="Agencia Tres Real")),
    (house("La Mairena", price=3_495_000, area=437, rooms=6,
           features=["aire acondicionado", "garaje", "jardin", "piscina", "terraza", "trastero"],
           address="Santa María, Marbella, Málaga", advertiser="Agencia Cuatro Sl", phone=PHONE),
     house("La Mairena", price=3_495_000, area=437, rooms=6,
           features=["aire acondicionado", "garaje", "jardin", "piscina", "terraza", "trastero"],
           address="Elviria - Cabopino, Marbella, Málaga", advertiser="Agencia Cinco Properties")),
]
BENCH = {"without_address": True, "district_not_street": True}  # the flags of scripts/sim/harness.py


def pair(a: dict, b: dict, *, url_a: str = FOTO.format(1), url_b: str = HAB.format(1), body_a: str = "",
         body_b: str = "", **flags) -> bool:
    la, lb = listing_of(a, url=url_a, body=body_a), listing_of(b, url=url_b, body=body_b)
    return same_object(la, lb, **flags) and same_object(lb, la, **flags)


@pytest.mark.parametrize("index", range(len(PAIRS)))
def test_bench_pairs_merge_only_with_the_flag(index: int) -> None:
    a, b = PAIRS[index]
    assert not pair(a, b, **BENCH)  # the bug: two cards
    # «Nueva Andalucía» is shared, but with Q15 its words are district stop words (``_DISTRICT_STOP``): no street left
    assert pair(a, b) is (index == 5)
    assert pair(a, b, **BENCH, cross_portal_facts=True)
    assert pair(a, b, cross_portal_facts=True)


def test_one_agency_two_units_do_not_merge() -> None:
    a, b = PAIRS[2]  # one advertiser on both portals, 4.5M
    flags = {**BENCH, "cross_portal_facts": True}
    assert not pair(a, {**b, "area_m2": 600}, **flags)  # another area
    assert not pair(a, {**b, "rooms": 4}, **flags)  # other rooms
    assert not pair(a, {**b, "rooms": None}, **flags)  # rooms unknown on one side
    assert not pair({**a, "rooms": None}, {**b, "rooms": None}, **flags)
    assert not pair(a, {**b, "price_amount": 4_400_000}, **flags)  # beyond 1 %
    assert not pair(a, b, url_b=FOTO.format(2), **flags)  # two ads of one portal
    assert not pair(a, {**b, "property_type": "apartment"}, **flags)


def test_new_development_and_references() -> None:
    a, b = PAIRS[2]
    flags = {**BENCH, "cross_portal_facts": True}
    promo = "Obra nueva en Marbella, villas de diseno con piscina."
    assert not pair(a, b, body_a=promo, body_b=promo, **flags)  # units of one project share the facts
    ref = 'JSON-LD: {{"agency_ref": "{}"}}\nVilla con piscina.'
    assert not pair(a, b, body_a=ref.format("NT-101"), body_b=ref.format("NT-102"), **flags)
    assert pair(a, b, body_a=ref.format("NT-101"), body_b=ref.format("NT-101"), **flags)


def test_streets_and_evidence() -> None:
    a, b = PAIRS[0]
    flags = {**BENCH, "cross_portal_facts": True}
    assert not pair({**a, "address": "Calle Uno 5, Marbella"}, {**b, "address": "Calle Dos, Marbella"}, **flags)
    # a street on one side and another district on the other
    assert not pair({**a, "address": "Calle Uno, Marbella"}, b, **flags)
    # no shared phone, agency or two features
    assert not pair({**a, "features": ["piscina"]}, {**b, "features": ["piscina"]}, **flags)
    # another city
    assert not pair(a, {**b, "location": "Milla de Oro, Estepona", "address": "Milla de Oro, Estepona"}, **flags)


def test_settings_pass_the_flag(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_CROSS_PORTAL_FACTS", raising=False)
    assert RunnerConfig().dedup_cross_portal_facts is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_cross_portal_facts is False
    monkeypatch.setenv("DEDUP_CROSS_PORTAL_FACTS", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_cross_portal_facts is True


GOAL = "Купить дом в Марбелье до 1000000 евро"


async def _cards(flag: bool) -> tuple[int, dict]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, dedup_without_address=True,
        dedup_district_not_street=True, dedup_cross_portal_facts=flag))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, link, payload in (("f1", FOTO.format(2), PAIRS[1][0]), ("f2", HAB.format(2), PAIRS[1][1])):
        pl = {**payload, "original_post_link": link, "summary_ru": "Дом"}
        store.add_finding(cid, fid, f"🏠 {fid}", payload=pl, original="", vertical="real_estate", url=link)
    await runner.step(cid)
    return len(messenger.findings()), store.duplicates


async def test_runner_sends_one_card_only_with_the_flag() -> None:
    assert await _cards(False) == (2, {})
    assert await _cards(True) == (1, {"f2": "f1"})
