"""Q8: one plot listed by several agencies on one portal, one copy without a street address (synthetic data:
the figures of the bench's case, invented texts, agencies and phones)."""

from __future__ import annotations

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import listing_of, same_object
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_runner import FakeMessenger

ONE = "https://www.fotocasa.es/es/comprar/terreno/madrid-capital/la-florida-el-plantio/100000001/d"
TWO = "https://www.fotocasa.es/es/comprar/terreno/madrid-capital/la-florida-el-plantio/100000002/d"
DISTRICT = "La Florida - El Plantío"

HEAD_BODY = ("Agencia Ejemplo ofrece solar urbano en Calle Gobelas de 2.519 m2, zona N. Z. 8.1. A, vivienda unifamiliar "
             "aislada, tres plantas. Contacto 600 111 222.")
COPY_BODY = ("Inmobiliaria Prueba vende parcela de 2519 m2 en urbanización de alto standing, norma zonal 8.1.A, "
             "uso residencial, hasta tres plantas. Llame al 600 333 444.")
IDEALISTA = "https://www.idealista.com/inmueble/100000003/"


def plot(*, address: str | None = None, district: str | None = DISTRICT, price: float = 1_550_000,
         area: float | None = 2519, deal: str = "sale", kind: str | None = "land", city: str = "Madrid",
         rooms: int | None = None, **extra) -> dict:
    location = f"{district}, {city}" if district else city
    return {"price_amount": price, "price_currency": "EUR", "area_m2": area, "deal_type": deal, "property_type": kind,
            "location": location, "district": district, "address": address, "rooms": rooms, **extra}


def pair(head: dict, copy: dict, head_body: str = HEAD_BODY, copy_body: str = COPY_BODY, copy_url: str = TWO):
    return listing_of(head, url=ONE, body=head_body), listing_of(copy, url=copy_url, body=copy_body)


def merged(*args, **kw) -> bool:
    a, b = pair(*args, **kw)
    return same_object(a, b, without_address=True) or same_object(b, a, without_address=True)


def test_two_copies_without_an_address_merge() -> None:
    head, copy = pair(plot(address="Calle Gobelas"), plot())
    assert same_object(head, copy, without_address=True) and same_object(copy, head, without_address=True)
    both_bare = pair(plot(), plot(price=1_545_000, area=2500))  # within 1 % and 2 %
    assert same_object(*both_bare, without_address=True)


def test_without_the_flag_the_copy_stays_a_second_card() -> None:
    assert not same_object(*pair(plot(address="Calle Gobelas"), plot()))
    assert not same_object(*pair(plot(), plot()))


def test_other_price_with_the_same_area_is_another_plot() -> None:
    assert not same_object(*pair(plot(), plot(price=1_350_000)), without_address=True)
    assert not same_object(*pair(plot(), plot(price=1_570_000)), without_address=True)  # 1.3 %: over 1 %


def test_other_district_is_another_plot() -> None:
    assert not same_object(*pair(plot(), plot(district="Aravaca")), without_address=True)


def test_area_type_deal_and_stated_marks_must_agree() -> None:
    assert not same_object(*pair(plot(), plot(area=2580)), without_address=True)  # 2.4 %: over 2 %
    assert not same_object(*pair(plot(), plot(kind="house")), without_address=True)
    assert not same_object(*pair(plot(kind=None), plot(kind=None)), without_address=True)  # type unknown
    assert not same_object(*pair(plot(), plot(deal="rent")), without_address=True)
    long_form = COPY_BODY.replace("norma zonal 8.1.A", "Norma Zonal 8, grado 1.º, nivel A")
    assert same_object(*pair(plot(), plot(), copy_body=long_form), without_address=True)
    other_zone = COPY_BODY.replace("8.1.A", "8.2.B")
    assert not same_object(*pair(plot(), plot(), copy_body=other_zone), without_address=True)
    assert not same_object(*pair(plot(), plot(), "Ref. Catastral: 1111111AA1111A0001AA",
                                 "Referencia catastral 2222222BB2222B0001BB"), without_address=True)
    # two different streets are never merged, by any rule
    assert not same_object(*pair(plot(address="Calle Gobelas"), plot(address="Calle Ficticia 9")),
                           without_address=True)


def test_a_city_is_enough_only_when_a_side_names_no_district() -> None:
    assert merged(plot(), plot(district=None))
    assert not merged(plot(), plot(district=None, city="Valencia"))
    assert not merged(plot(), plot(city="Valencia"))  # the city is always compared, even with a shared district


# Review cases: none of them is one object.

def test_review_1_new_development_on_one_portal() -> None:
    flat = {"kind": "apartment", "rooms": 2, "area": 85, "price": 250_000, "district": "Ruzafa", "city": "Valencia"}
    assert not merged(plot(**flat), plot(**flat), "Piso de obra nueva", "Piso de obra nueva")
    body = "Promoción de parcelas, norma zonal 8.1.A, hasta tres plantas. Llame al 600 555 666."
    assert not merged(plot(), plot(), body, COPY_BODY)
    assert not merged(plot(), plot(), "Residencial Los Robles: parcela N.Z. 8.1.A, tres plantas.", COPY_BODY)
    assert not merged(plot(), plot(location=f"Obra nueva, {DISTRICT}, Madrid"))


def test_review_2_a_floor_or_number_only_in_the_text() -> None:
    assert not merged(plot(), plot(), "Parcela 1 del sector, N.Z. 8.1.A", "Parcela 3 del sector, N.Z. 8.1.A")
    flat = {"kind": "apartment", "rooms": 2, "area": 85, "price": 250_000, "district": "Ruzafa", "city": "Valencia"}
    assert not merged(plot(**flat), plot(**flat), "Piso en planta 1, puerta 2", "Piso en planta 3, puerta 5")


def test_review_3_one_advertiser_on_one_portal() -> None:
    same_phone = COPY_BODY.replace("600 333 444", "600 111 222")
    assert not merged(plot(), plot(), copy_body=same_phone)
    assert not merged(plot(advertiser="Agencia Ejemplo S.L."), plot(advertiser="AGENCIA EJEMPLO"))
    assert not merged(plot(phone="600 777 888"), plot(phone="+34 600 777 888"))
    assert merged(plot(), plot(), copy_body=same_phone, copy_url=IDEALISTA)  # two portals: one agency, one plot


def test_review_4_generic_district_words_and_regions() -> None:
    assert not merged(plot(district="Urbanización Los Pinos"), plot(district="Urbanización El Bosque"))
    assert not merged(plot(district="Costa del Sol", city="Marbella"), plot(district="Costa del Sol", city="Estepona"))
    assert not merged(plot(district="Residencial Norte"), plot(district="Residencial Sur"))
    assert not merged(plot(district=None, city="Sevilla, Andalucía"), plot(district=None, city="Granada, Andalucía"))


def test_review_5_typical_plots_of_one_estate() -> None:
    estate = {"district": "Urbanización Los Pinos", "city": "Valencia", "price": 120_000}
    body = "Parcela urbana en urbanización con piscina comunitaria."
    assert not merged(plot(area=1000, **estate), plot(area=1000, **estate), body, body)
    assert not merged(plot(area=None, plot_m2=1000, **estate), plot(area=None, plot_m2=1000, **estate), body, body)
    assert not merged(plot(area=1000, **estate), plot(area=1010, **estate), body, body)
    # a zone in a text: the area may be round
    zoned = "Parcela urbana, N.Z. 5.2.B."
    assert merged(plot(area=1000, **estate), plot(area=1000, **estate), zoned, zoned)


def test_review_6_rent_rooms_other_kinds_and_flats() -> None:
    assert not merged(plot(deal="rent", price=1200), plot(deal="rent", price=1200))
    for kind in ("room", "other", "apartment", "house", None):
        assert not merged(plot(kind=kind), plot(kind=kind))
    flat = {"kind": "apartment", "area": 85, "price": 250_000, "district": "Ruzafa", "city": "Valencia"}
    assert not merged(plot(rooms=None, **flat), plot(rooms=None, **flat), "Piso", "Piso")


def test_flag_is_off_by_default_and_reaches_the_runner_config(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_WITHOUT_ADDRESS", raising=False)
    assert RunnerConfig().dedup_without_address is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_without_address is False
    monkeypatch.setenv("DEDUP_WITHOUT_ADDRESS", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_without_address is True


LAND_GOAL = "Купить участок в Мадриде от 2000 м²"


async def _cards(flag: bool) -> tuple[int, dict]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, dedup_without_address=flag))
    cid = await campaigns.create(plan_campaign(LAND_GOAL), chat_id=-100, requested_by=42, source_text=LAND_GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, link, address, body in (("f1", ONE, "Calle Gobelas", HEAD_BODY), ("f2", TWO, None, COPY_BODY)):
        pl = {**plot(address=address), "original_post_link": link, "summary_ru": "Участок"}
        store.add_finding(cid, fid, f"🏠 {fid}", payload=pl, original=body, vertical="real_estate", url=link)
    await runner.step(cid)
    return len(messenger.findings()), store.duplicates


async def test_runner_sends_one_card_only_with_the_flag() -> None:
    assert await _cards(False) == (2, {})
    assert await _cards(True) == (1, {"f2": "f1"})
