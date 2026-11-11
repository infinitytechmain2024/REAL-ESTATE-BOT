"""Q8: one plot listed by several agencies on one portal, one copy without a street address (synthetic data:
the figures of the bench's case, invented texts, agencies and phones)."""

from __future__ import annotations

from bot.campaign.dedup import listing_of, same_object
from bot.campaign.runner import RunnerConfig
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_dedup import payload, setup

ONE = "https://www.fotocasa.es/es/comprar/terreno/madrid-capital/la-florida-el-plantio/100000001/d"
TWO = "https://www.fotocasa.es/es/comprar/terreno/madrid-capital/la-florida-el-plantio/100000002/d"
DISTRICT = "La Florida - El Plantío"

HEAD_BODY = ("Agencia Ejemplo ofrece solar urbano en Calle Gobelas de 2.519 m2, zona N. Z. 8.1. A, vivienda unifamiliar "
             "aislada, tres plantas. Contacto 600 111 222.")
COPY_BODY = ("Inmobiliaria Prueba vende parcela de 2519 m2 en urbanización de alto standing, norma zonal 8.1.A, "
             "edificabilidad para una vivienda. Llame al 600 333 444.")


def plot(*, address: str | None = None, district: str = DISTRICT, price: float = 1_550_000, area: float = 2519,
         deal: str = "sale", kind: str = "land") -> dict:
    return {"price_amount": price, "price_currency": "EUR", "area_m2": area, "deal_type": deal, "property_type": kind,
            "location": f"{district}, Madrid", "district": district, "address": address, "rooms": None}


def pair(head: dict, copy: dict, head_body: str = HEAD_BODY, copy_body: str = COPY_BODY):
    return listing_of(head, url=ONE, body=head_body), listing_of(copy, url=TWO, body=copy_body)


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
    assert same_object(*pair(plot(), plot(), copy_body=COPY_BODY.replace("8.1.A", "8")), without_address=True)
    other_zone = COPY_BODY.replace("8.1.A", "8.2.B")
    assert not same_object(*pair(plot(), plot(), copy_body=other_zone), without_address=True)
    assert not same_object(*pair(plot(), plot(), "Ref. Catastral: 1111111AA1111A0001AA",
                                 "Referencia catastral 2222222BB2222B0001BB"), without_address=True)
    # two different streets are never merged, by any rule
    assert not same_object(*pair(plot(address="Calle Gobelas"), plot(address="Calle Ficticia 9")),
                           without_address=True)


def test_a_city_is_enough_only_when_a_side_names_no_district() -> None:
    city_only = {**plot(), "location": "Madrid", "district": None}
    assert same_object(*pair(plot(), city_only), without_address=True)
    elsewhere = {**plot(), "location": "Valencia", "district": None}
    assert not same_object(*pair(plot(), elsewhere), without_address=True)


def test_flag_is_off_by_default_and_reaches_the_runner_config(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_WITHOUT_ADDRESS", raising=False)
    assert RunnerConfig().dedup_without_address is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_without_address is False
    monkeypatch.setenv("DEDUP_WITHOUT_ADDRESS", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_without_address is True


def _flat(store, cid: str, fid: str, link: str, price: float, area: float, address: str | None) -> None:
    pl = {**payload(price, area, link=link), "property_type": "apartment", "address": address, "district": "Ruzafa"}
    store.add_finding(cid, fid, f"🏠 {fid}", payload=pl, original=f"post {fid}", vertical="real_estate", url=link)


async def _cards(flag: bool) -> tuple[int, dict]:
    store, messenger, runner, cid = await setup()
    runner.config = RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0, dedup_without_address=flag)
    _flat(store, cid, "f1", "https://www.fotocasa.es/es/comprar/vivienda/valencia/100000011/d", 199_000, 85,
          "Calle Ficticia 3")
    _flat(store, cid, "f2", "https://www.fotocasa.es/es/comprar/vivienda/valencia/100000012/d", 199_500, 86, None)
    await runner.step(cid)
    return len(messenger.findings()), store.duplicates


async def test_runner_sends_one_card_only_with_the_flag() -> None:
    assert await _cards(False) == (2, {})
    assert await _cards(True) == (1, {"f2": "f1"})
