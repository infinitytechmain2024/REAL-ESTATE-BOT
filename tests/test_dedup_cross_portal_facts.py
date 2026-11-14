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


ON = {**BENCH, "cross_portal_facts": True}
DESCRIPTION = ("Villa contemporanea con vistas al mar, salon de doble altura, cocina abierta equipada, cinco suites con "
               "bano, gimnasio, bodega, jardin con piscina desbordante, garaje para tres coches, a pocos minutos de la "
               "playa y de los campos de golf.")
OTHER = ("Casa adosada en urbanizacion cerrada con zonas comunes, salon con chimenea, cocina independiente, tres "
         "dormitorios, solarium en la planta alta, plaza de garaje y trastero en el sotano, cerca de colegios.")


def post(url: str, description: str, ref: str | None = None, title: str = "Casa en venta, Marbella") -> str:
    """A portal post as ``web_search.worker._source_text`` writes it (the «JSON-LD» line and the title differ per
    portal: Fotocasa «Casa en venta, <district>», Habitaclia the agency's own title)."""
    facts = f'"url": "{url}", "title": "{title}"' + (f', "agency_ref": "{ref}"' if ref else "")
    return f"JSON-LD: {{{facts}}}\n{title}\nСсылка: {url}\n{description}"


HAB_TITLE = "Exclusiva villa moderna a 300 m de la playa con 2 piscinas"


@pytest.mark.parametrize("index", range(len(PAIRS)))
def test_bench_pairs_merge_only_with_the_flag(index: int) -> None:
    a, b = PAIRS[index]
    # the bench bodies: one description and one reference, weak (4 digits: no ``strong_ref``, Q15 does not merge)
    kw = {"body_a": post(FOTO.format(1), DESCRIPTION, "1496"), "body_b": post(HAB.format(1), DESCRIPTION, "1496", HAB_TITLE)}
    assert not pair(a, b, **BENCH, **kw)  # the bug: two cards
    assert pair(a, b, **ON, **kw)
    assert pair(a, b, cross_portal_facts=True, **kw)


@pytest.mark.parametrize("index", range(len(PAIRS)))
def test_bench_pairs_by_the_payload_alone(index: int) -> None:
    """Without a body only the agency names are a sign; «Fastighetsformedling» and «Gr8 Real Estate» share none and
    their shared features no longer merge (pair 11/18 merges by its reference or description, above)."""
    a, b = PAIRS[index]
    assert not pair(a, b, **BENCH)
    # «Nueva Andalucía» is shared, but with Q15 its words are district stop words (``_DISTRICT_STOP``): no street left
    assert pair(a, b) is (index == 5)
    assert pair(a, b, **ON) is (index != 6)


def test_each_strong_sign_alone() -> None:
    a, b = PAIRS[6]  # two agency names without a shared word
    assert not pair(a, b, **ON)
    assert pair(a, {**b, "phone": PHONE}, **ON)
    assert pair(a, b, body_a=post(FOTO.format(1), OTHER, "GR8-04051"), body_b=post(HAB.format(1), DESCRIPTION, "gr8 04051", HAB_TITLE), **ON)
    assert pair(a, b, body_a=post(FOTO.format(1), DESCRIPTION), body_b=post(HAB.format(1), DESCRIPTION, title=HAB_TITLE),
                **ON)
    assert not pair(a, b, body_a=post(FOTO.format(1), DESCRIPTION), body_b=post(HAB.format(1), OTHER), **ON)
    # another floor in one template text: another flat
    assert not pair(a, b, body_a=post(FOTO.format(1), "Planta 2. " + DESCRIPTION),
                    body_b=post(HAB.format(1), "Planta 5. " + DESCRIPTION), **ON)
    # generic words of two agency names are no sign («Inmobiliaria», «Real Estate», a city)
    assert not pair({**a, "advertiser": "Inmobiliaria Marbella"}, {**b, "advertiser": "MARBELLA REAL ESTATE"}, **ON)
    assert not pair({**a, "advertiser": "Inmobiliaria"}, {**b, "advertiser": "INMOBILIARIA"}, **ON)


def test_bench_false_pairs_do_not_merge() -> None:
    """The bench's own false pairs (habitaclia rows, invented names and references): only the facts and features agree."""
    features = ["aire acondicionado", "garaje", "piscina"]
    town = house("Casco Antiguo", price=850_000, area=207, rooms=3, features=features, advertiser="Agencia Seis")
    villa = house("Las Chapas - El Rosario", price=850_000, area=210, rooms=3, features=features,
                  advertiser="Agencia Siete Invest")
    assert not pair(town, villa, **ON)
    assert not pair(town, villa, body_a=post(FOTO.format(1), OTHER, "RR2705-AA"),
                    body_b=post(HAB.format(1), DESCRIPTION, "14889"), **ON)

    def rent(district: str, advertiser: str) -> dict:
        return {"price_amount": 1200, "price_currency": "EUR", "area_m2": 60, "rooms": 1, "deal_type": "rent",
                "property_type": "apartment", "district": district, "location": f"{district}, Madrid",
                "address": f"{district}, Madrid", "advertiser": advertiser, "features": ["ascensor", "parquet"]}
    assert not pair(rent("Ciudad Lineal", "Agencia Ocho"), rent("Barrio de Salamanca", "Agencia Nueve"), **ON,
                    url_b="https://www.idealista.com/inmueble/5/")


def test_a_round_price_must_be_equal() -> None:
    a = house("Nueva Andalucía", price=999_000, area=300, rooms=4, features=["piscina", "terraza"], advertiser="Promo Dev")
    b = house("Elviria", price=995_000, area=305, rooms=4, features=["piscina", "terraza"], advertiser="Promo Dev SL")
    assert not pair(a, b, **ON)  # 0.4 %: within 1 %, still two villas of one developer
    assert pair(a, {**b, "price_amount": 999_000}, **ON)
    # a price that is not round: within 0.5 %
    assert pair({**a, "price_amount": 999_950}, {**b, "price_amount": 998_450}, **ON)
    assert not pair({**a, "price_amount": 999_950}, {**b, "price_amount": 993_950}, **ON)


def test_the_type_must_be_known_on_both_sides() -> None:
    a, b = PAIRS[2]
    assert not pair(a, {**b, "property_type": None}, **ON)
    assert not pair({**a, "property_type": None}, {**b, "property_type": None}, **ON)
    assert not pair(a, {**b, "property_type": "apartment"}, **ON)


def test_one_agency_two_units_do_not_merge() -> None:
    a, b = PAIRS[2]  # one advertiser on both portals, 4.5M
    assert not pair(a, {**b, "area_m2": 600}, **ON)  # another area
    assert not pair(a, {**b, "rooms": 4}, **ON)  # other rooms
    assert not pair(a, {**b, "rooms": None}, **ON)  # rooms unknown on one side
    assert not pair({**a, "rooms": None}, {**b, "rooms": None}, **ON)
    assert not pair(a, b, url_b=FOTO.format(2), **ON)  # two ads of one portal


def test_new_development_and_references() -> None:
    a, b = PAIRS[2]
    promo = "Obra nueva en Marbella, villas de diseno con piscina."
    assert not pair(a, b, body_a=promo, body_b=promo, **ON)  # units of one project share the facts
    assert not pair(a, b, body_a=post(FOTO.format(1), DESCRIPTION, "NT-101"),
                    body_b=post(HAB.format(1), DESCRIPTION, "NT-102"), **ON)
    assert pair(a, b, body_a=post(FOTO.format(1), DESCRIPTION, "NT-101"),
                body_b=post(HAB.format(1), DESCRIPTION, "NT-101"), **ON)


def test_streets_and_city() -> None:
    a, b = PAIRS[0]
    assert not pair({**a, "address": "Calle Uno 5, Marbella"}, {**b, "address": "Calle Dos, Marbella"}, **ON)
    # a street on one side and another district on the other
    assert not pair({**a, "address": "Calle Uno, Marbella"}, b, **ON)
    assert not pair(a, {**b, "location": "Milla de Oro, Estepona", "address": "Milla de Oro, Estepona"}, **ON)


def test_settings_pass_the_flag(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_CROSS_PORTAL_FACTS", raising=False)
    assert RunnerConfig().dedup_cross_portal_facts is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_cross_portal_facts is False
    monkeypatch.setenv("DEDUP_CROSS_PORTAL_FACTS", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_cross_portal_facts is True


GOAL = "Купить дом в Марбелье до 4000000 евро"


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
    for fid, link, payload, title in (("f1", FOTO.format(2), PAIRS[6][0], "Casa en venta, La Mairena"),
                                      ("f2", HAB.format(2), PAIRS[6][1], HAB_TITLE)):
        pl = {**payload, "original_post_link": link, "summary_ru": "Дом"}  # pair 11/18: only the description is a sign
        store.add_finding(cid, fid, f"🏠 {fid}", payload=pl, original=post(link, DESCRIPTION, title=title), vertical="real_estate",
                          url=link)
    await runner.step(cid)
    return len(messenger.findings()), store.duplicates


async def test_runner_sends_one_card_only_with_the_flag() -> None:
    assert await _cards(False) == (2, {})
    assert await _cards(True) == (1, {"f2": "f1"})
