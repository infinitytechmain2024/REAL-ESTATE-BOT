"""Q15: Fotocasa and Habitaclia list one flat with the district named differently in the «address» (synthetic data:
the shape of the bench's Valencia case, invented texts, agencies and references; mocks only)."""

from __future__ import annotations

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import (
    _DISTRICT_STOP,
    _PLACE_STOP,
    has_street,
    listing_of,
    ref_key,
    same_object,
    strong_ref,
)
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.portals import ApifyFotocasaSource, ApifyHabitacliaSource
from bot.web_search.structured import facts_block
from tests.test_campaign_runner import FakeMessenger

FOTO = "https://www.fotocasa.es/es/comprar/vivienda/valencia-capital/ascensor/100000001/d"
FOTO_2 = "https://www.fotocasa.es/es/comprar/vivienda/valencia-capital/ascensor/100000002/d"
HAB = "https://www.habitaclia.com/i10000000000001.htm"
TEXT = ("Piso exterior de dos dormitorios con ascensor y balcon, cocina reformada, bano completo, aire acondicionado, "
        "muy luminoso, cerca del metro y de colegios, ideal para entrar a vivir.")
OTHER_TEXT = ("Vivienda amplia en finca con portero, salon comedor, terraza, calefaccion, armarios empotrados, parquet, "
              "trastero incluido, orientacion sur, zona tranquila con comercios.")


def flat(district: str, *, city: str = "Valencia", address: str | None = None, price: float = 199_000,
         area: float | None = 70, rooms: int | None = 2, floor: int | None = 1, **extra) -> dict:
    return {"price_amount": price, "price_currency": "EUR", "area_m2": area, "deal_type": "sale",
            "property_type": "apartment", "rooms": rooms, "floor": floor, "district": district,
            "location": f"{district}, {city}", "address": address or f"{district}, {city}", **extra}


def body(text: str, ref: str | None = None) -> str:
    """The post as web_search/worker.py ``_source_text`` writes it: the «JSON-LD» line, the title, the description."""
    facts = {"url": "x", "title": "Vivienda en venta", "price": 199000.0, "currency": "EUR"}
    if ref is not None:
        facts["agency_ref"] = ref
    return f"{facts_block(facts)}\nVivienda en venta\nСсылка: x\n{text}"


def merged(a: dict, b: dict, *, flag: bool, url_a: str = FOTO, url_b: str = HAB, body_a: str = "",
           body_b: str = "") -> bool:
    one, two = listing_of(a, url=url_a, body=body_a), listing_of(b, url=url_b, body=body_b)
    left = same_object(one, two, district_not_street=flag)
    assert left == same_object(two, one, district_not_street=flag)
    return left


FEATURES = {"features": ["ascensor", "balcon", "exterior"]}


def test_same_facts_in_two_districts_merge_only_by_reference_or_text() -> None:
    a = flat("L'Amistat", **FEATURES)
    b = flat("Algirós", city="Valencia Capital", **FEATURES)
    assert not merged(a, b, flag=False) and not merged(a, b, flag=True)  # two shared features are not enough
    refs = {"body_a": body(TEXT, "LCV-45579-1"), "body_b": body(OTHER_TEXT, "LCV-45579-1")}
    assert not merged(a, b, flag=False, **refs)
    assert merged(a, b, flag=True, **refs)


def test_the_same_text_in_two_districts_merges_only_with_the_flag() -> None:
    a, b = flat("La Torre", price=177_500, area=85, rooms=3, floor=3), flat("Pobles del Sud", price=177_500, area=85,
                                                                         rooms=3, floor=3, city="Valencia Capital")
    assert not merged(a, b, flag=False, body_a=TEXT, body_b=TEXT)
    assert merged(a, b, flag=True, body_a=TEXT, body_b=TEXT)


def test_the_same_agency_reference_with_price_and_area_merges() -> None:
    a, b = flat("Na Rovella - Hermanos Maristas"), flat("Quatre Carreres", city="Valencia Capital")
    kw = {"body_a": body(TEXT, "ABC-123-45"), "body_b": body(OTHER_TEXT, "abc 123 45")}
    assert not merged(a, b, flag=False, **kw)
    assert merged(a, b, flag=True, **kw)
    # the reference alone is not enough: the price and the area must match too
    assert not merged(a, flat("Quatre Carreres", price=210_000), flag=True, **kw)
    assert not merged(a, flat("Quatre Carreres", area=None), flag=True, **kw)
    assert not merged(a, flat("Quatre Carreres", area=90), flag=True, **kw)
    # one portal never lists a flat twice
    assert not merged(a, b, flag=True, url_b=FOTO_2, **kw)


def test_one_agency_two_flats_in_neighbouring_districts_stay_two() -> None:
    a = flat("Benicalap", agency="Inmobiliaria Ejemplo", **FEATURES)
    b = flat("Torrefiel", city="Valencia Capital", agency="Inmobiliaria Ejemplo", **FEATURES)
    assert not merged(a, b, flag=True)  # another district, no street: the facts rule does not apply
    kw = {"body_a": body(TEXT, "REF-0001"), "body_b": body(TEXT, "REF-0002")}
    assert not merged(a, b, flag=True, **kw)  # another reference: another flat, even with the same text
    # streets known and shared, one advertiser, two references: two flats of one building
    on_street = {"address": "Calle Ejemplo, Benicalap, Valencia"}
    assert not merged({**a, **on_street}, {**b, **on_street}, flag=True, **kw)


def test_two_streets_never_merge() -> None:
    a = flat("Benicalap", address="Calle Uno, Benicalap, Valencia", **FEATURES)
    b = flat("Torrefiel", address="Avenida Dos, Torrefiel, Valencia Capital", **FEATURES)
    kw = {"body_a": body(TEXT, "SAME-9"), "body_b": body(TEXT, "SAME-9")}
    assert not merged(a, b, flag=False, **kw)
    assert not merged(a, b, flag=True, **kw)


def test_other_facts_are_other_flats() -> None:
    a = flat("L'Amistat", **FEATURES)
    for other in (flat("Algirós", price=205_000, **FEATURES), flat("Algirós", rooms=3, **FEATURES),
                  flat("Algirós", floor=4, **FEATURES), flat("Algirós", city="Madrid", **FEATURES),
                  flat("Algirós", features=["piscina"])):
        assert not merged(a, other, flag=True)


def test_capital_is_no_shared_place() -> None:
    # «Valencia Capital» on both sides: "capital" is a district stop word, the districts are not shared
    a, b = flat("Benimaclet", city="Valencia Capital"), flat("Patraix", city="Valencia Capital")
    assert not merged(a, b, flag=True, url_b="https://www.pisos.com/comprar/piso-valencia-100000001/")


IDE = "https://www.idealista.com/inmueble/100000001/"
TWO = {"features": ["ascensor", "exterior"]}


def test_review_1_other_agencies_other_districts_same_facts_stay_two() -> None:
    a, b = flat("Benicalap", agency="Agencia A", **TWO), flat("Torrefiel", agency="Agencia B", **TWO)
    assert not merged(a, b, flag=True, url_a=IDE, url_b=FOTO, body_a=TEXT, body_b=OTHER_TEXT)
    # a street on one side, another district on the other: never, not even by the text
    street = flat("Benicalap", address="Calle Uno, Benicalap, Valencia", agency="Agencia A", **TWO)
    assert not merged(street, b, flag=True, url_a=IDE, url_b=FOTO, body_a=TEXT, body_b=OTHER_TEXT)
    assert not merged(street, b, flag=True, url_a=IDE, url_b=FOTO, body_a=TEXT, body_b=TEXT)


def test_review_2_a_reference_on_one_side_only_is_no_reference() -> None:
    a, b = flat("Benicalap", agency="Agencia A"), flat("Torrefiel", agency="Agencia A")
    assert not merged(a, b, flag=True, url_a=IDE, url_b=FOTO, body_a=TEXT, body_b=body(OTHER_TEXT, "REF-1"))
    assert not merged(a, b, flag=True, url_a=FOTO, url_b=HAB, body_a=body(TEXT), body_b=body(OTHER_TEXT, "ABC-123"))


@pytest.mark.parametrize("ref", ["2011", "1999", "TORREFIEL", "1234"])
def test_review_3_4_weak_references_do_not_merge(ref: str) -> None:
    a, b = flat("Benicalap", agency="A"), flat("Torrefiel", agency="B")
    assert not merged(a, b, flag=True, body_a=body(TEXT, ref), body_b=body(OTHER_TEXT, ref))
    assert not strong_ref(ref_key(ref))


def test_strong_references() -> None:
    for ref in ("LCV45579-1", "REO-111-860", "12345", "46-3501588", "Fran1111", "PIS2087"):
        assert strong_ref(ref_key(ref)), ref


def test_review_5_a_new_development_never_merges_by_reference() -> None:
    promo = "Obra nueva, promoción de viviendas. "
    a, b = flat("Malilla", agency="Promotora"), flat("La Fonteta", agency="Promotora")
    kw = {"body_a": body(promo + TEXT, "PROMO-77"), "body_b": body(promo + OTHER_TEXT, "PROMO-77")}
    assert not merged(a, b, flag=True, **kw)
    assert merged(a, b, flag=True, body_a=body(TEXT, "PROMO-77"), body_b=body(OTHER_TEXT, "PROMO-77"))


def test_review_7_one_street_one_district_same_agency_stay_two() -> None:
    a = flat("Benicalap", address="Calle Uno, Benicalap, Valencia", agency="A")
    assert not merged(a, flat("Torrefiel", agency="A"), flag=True, url_a=IDE, url_b=FOTO, body_a=TEXT,
                      body_b=OTHER_TEXT)


def test_rooms_known_on_one_side_only_do_not_merge_by_reference() -> None:
    a, b = flat("Benicalap", rooms=None, agency="A"), flat("Torrefiel", agency="B")
    assert not merged(a, b, flag=True, body_a=body(TEXT, "12345"), body_b=body(OTHER_TEXT, "12345"))


def test_a_district_named_like_a_street_is_a_district() -> None:
    # «Camí Fondo» is a district of Valencia: Fotocasa's address is the district, not a street
    a = flat("Camí Fondo", price=180_000, area=64, rooms=3, floor=0)
    b = flat("Camins al Grau", city="Valencia Capital", price=180_000, area=64, rooms=3, floor=0)
    assert merged(a, b, flag=True, body_a=body(TEXT, "LCV45436-1"), body_b=body(OTHER_TEXT, "LCV45436-1"))
    street = flat("Benicalap", address="Camí de Moncada, Benicalap, Valencia")
    assert not merged(street, b, flag=True, body_a=body(TEXT, "LCV45436-1"), body_b=body(OTHER_TEXT, "LCV45436-1"))


LAND = {"price_amount": 100000, "price_currency": "EUR", "deal_type": "sale", "property_type": "land",
        "plot_m2": 733, "district": "", "address": ""}


@pytest.mark.parametrize(("one", "two", "before_q15"), [
    ("Valencia Capital", "Benimaclet, Valencia", False),
    ("Benimaclet, Valencia Capital", "Patraix, Valencia Capital", True),  # "capital" is shared on main
])
def test_q8_without_q15_is_as_on_main(one: str, two: str, before_q15: bool) -> None:
    x, y = listing_of({**LAND, "location": one}, url=FOTO), listing_of({**LAND, "location": two}, url=HAB)
    assert same_object(x, y, without_address=True) is before_q15
    assert "capital" not in _PLACE_STOP and "capital" in _DISTRICT_STOP
    if before_q15:  # with Q15 «capital» is no district: Benimaclet and Patraix are two plots
        assert not same_object(x, y, without_address=True, district_not_street=True)


def test_has_street_and_ref_key() -> None:
    for address in ("Calle Mayor, Valencia", "C/ Mayor, Valencia", "Av. del Puerto, Valencia", "Carrer de Sant Vicent",
                    "Plaça Redona", "Plaza Mayor", "Benicalap 12, Valencia"):
        assert has_street(address), address
    for address in ("L'Amistat, Valencia", "Algirós, Valencia Capital", "Na Rovella - Hermanos Maristas, Valencia",
                    "Barrio de Benicalap, Valencia", "Valencia", ""):
        assert not has_street(address), address
    assert ref_key("REO-111-860") == ref_key("reo 111 860") == "reo111860"
    assert ref_key("A1") == "" and ref_key(None) == ""


def test_the_reference_is_no_detail_of_the_flat() -> None:
    with_ref, without = listing_of(flat("X"), body=body(TEXT, "REO-111-860")), listing_of(flat("X"), body=body(TEXT))
    assert with_ref.agency_ref == "reo111860" and without.agency_ref == ""
    assert with_ref.numbers == without.numbers and with_ref.shingles == without.shingles
    assert listing_of(flat("X"), body=TEXT + ' "agency_ref": "Z-1"').agency_ref == ""  # only the «JSON-LD» line


TASK = QueryTask("piso", "piso", "Valencia", {}, "real_estate", constraints={"deal": "sale"}, country_code="ES")


def fotocasa_row(**kw) -> dict:
    return {"url": FOTO, "transaction": {"type": "SALE", "price": 199000}, "surface": 70, "rooms": 2,
            "agency": {"name": "Agencia Uno"}, "agencyReference": "ABC-123", "description": TEXT,
            "location": {"level2Name": "Valencia", "level5Name": "Valencia", "level8Name": "L'Amistat"}, **kw}


def habitaclia_row(**kw) -> dict:
    return {"detailUrl": HAB, "title": "Piso en Valencia", "price": 199000, "areaSqm": 70, "rooms": 2,
            "city": "Valencia", "province": "Valencia", "location": "Algirós, Valencia Capital",
            "transactionType": "sale", "propertyType": "flat", "advertiserName": "Agencia Uno", "agencyRef": "ABC-123",
            "description": TEXT, **kw}


def test_portals_pass_the_reference_only_with_the_flag() -> None:
    assert ApifyFotocasaSource("secret-token")._normalize(fotocasa_row(), TASK, None).agency_ref is None
    assert ApifyHabitacliaSource("secret-token")._normalize(habitaclia_row(), TASK, None).agency_ref is None
    assert ApifyFotocasaSource("secret-token", agency_ref=True)._normalize(fotocasa_row(), TASK, None).agency_ref \
        == "ABC-123"
    assert ApifyHabitacliaSource("secret-token", agency_ref=True)._normalize(habitaclia_row(), TASK, None).agency_ref \
        == "ABC-123"
    source = ApifyFotocasaSource("secret-token", agency_ref=True)
    assert source._normalize(fotocasa_row(agencyReference=677200), TASK, None).agency_ref == "677200"
    assert source._normalize(fotocasa_row(agencyReference=" "), TASK, None).agency_ref is None
    assert source._normalize(fotocasa_row(agencyReference=None), TASK, None).agency_ref is None


@pytest.mark.asyncio
async def test_settings_pass_the_flag(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_DISTRICT_NOT_STREET", raising=False)
    assert RunnerConfig().dedup_district_not_street is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_district_not_street is False
    assert WebSearchSettings(_env_file=None).portal_agency_ref is False
    monkeypatch.setenv("DEDUP_DISTRICT_NOT_STREET", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_district_not_street is True
    settings = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", APIFY_FOTOCASA_ENABLED=True,
                                 APIFY_HABITACLIA_ENABLED=True)
    sources = settings.sources()
    try:
        assert {type(s) for s in sources} == {ApifyFotocasaSource, ApifyHabitacliaSource}
        assert all(s.agency_ref is True for s in sources)
    finally:
        for s in sources:
            await s.aclose()


GOAL = "Купить квартиру в Валенсии до 250000 евро"


async def _cards(flag: bool) -> tuple[int, dict]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, dedup_district_not_street=flag))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, link, district, city, text in (("f1", FOTO, "L'Amistat", "Valencia", body(TEXT, "ABC-123")),
                                            ("f2", HAB, "Algirós", "Valencia Capital", body(OTHER_TEXT, "ABC-123"))):
        pl = {**flat(district, city=city), "original_post_link": link, "summary_ru": "Квартира"}
        store.add_finding(cid, fid, f"🏠 {fid}", payload=pl, original=text, vertical="real_estate", url=link)
    await runner.step(cid)
    return len(messenger.findings()), store.duplicates


async def test_runner_sends_one_card_only_with_the_flag() -> None:
    assert await _cards(False) == (2, {})
    assert await _cards(True) == (1, {"f2": "f1"})


def test_guard_a_a_calle_named_like_its_district_is_a_street() -> None:
    a = flat("Colón", address="Calle Colón, Valencia")
    b = flat("Xàtiva", address="Calle Xàtiva, Valencia")
    for kw in ({"body_a": TEXT, "body_b": TEXT}, {"body_a": body(TEXT, "AB-12345"), "body_b": body(TEXT, "AB-12345")}):
        assert not merged(a, b, flag=False, url_a=IDE, url_b=FOTO, **kw)
        assert not merged(a, b, flag=True, url_a=IDE, url_b=FOTO, **kw)
    # «Camí Fondo» (a district word) with that district is still a district
    camí = flat("Camí Fondo", address="Camí Fondo, Valencia")
    assert merged(camí, flat("Algirós"), flag=True, url_a=FOTO, url_b=HAB, body_a=body(TEXT, "AB-12345"),
                  body_b=body(OTHER_TEXT, "ab12345"))


def test_guard_b_the_same_text_of_a_new_development_does_not_merge_without_a_street() -> None:
    a, b = flat("Benicalap"), flat("Torrefiel")
    assert merged(a, b, flag=True, url_a=IDE, url_b=FOTO, body_a=TEXT, body_b=TEXT)
    promo = "Obra nueva en Valencia. " + TEXT
    assert not merged(a, b, flag=True, url_a=IDE, url_b=FOTO, body_a=promo, body_b=promo)


def test_guard_c_two_agencies_do_not_merge_by_one_reference() -> None:
    kw = {"body_a": body(TEXT, "V001"), "body_b": body(OTHER_TEXT, "V001")}
    assert not merged(flat("Benicalap", agency="A"), flat("Torrefiel", agency="B"), flag=True, **kw)
    assert merged(flat("Benicalap", agency="A"), flat("Torrefiel", agency="A"), flag=True, **kw)
    assert merged(flat("Benicalap", agency="A"), flat("Torrefiel"), flag=True, **kw)  # one agency unknown


DAYA_1 = "https://www.fotocasa.es/es/comprar/terreno/daya-nueva/todas-las-zonas/100000011/d"
DAYA_2 = "https://www.fotocasa.es/es/comprar/terreno/daya-nueva/todas-las-zonas/100000012/d"
DAYA_TEXT = ("Finca rustica de regadio en Daya Nueva con pozo, casa de aperos y arboles frutales, acceso asfaltado, "
             "luz cerca, ideal para cultivo o recreo.")


def daya(agency: str, **extra) -> dict:
    return {"price_amount": 95_000, "price_currency": "EUR", "deal_type": "sale", "property_type": "land",
            "plot_m2": 10_234, "district": "", "address": "", "location": "Daya Nueva, Alicante", "agency": agency, **extra}


def daya_merged(a: dict, b: dict, ref_a: str, ref_b: str, *, flag: bool) -> bool:
    one = listing_of(a, url=DAYA_1, body=body(DAYA_TEXT, ref_a))
    two = listing_of(b, url=DAYA_2, body=body(DAYA_TEXT, ref_b))
    left = same_object(one, two, without_address=True, district_not_street=flag)
    assert left == same_object(two, one, without_address=True, district_not_street=flag)
    return left


def test_q25_one_plot_of_two_agencies_with_their_own_references_merges() -> None:
    a, b = daya("Agencia Uno"), daya("Inmobiliaria Dos")
    assert daya_merged(a, b, "AU-10234", "ID-55871", flag=False)
    assert daya_merged(a, b, "AU-10234", "ID-55871", flag=True)  # two agencies number their ads each their own way


def test_q25_one_agency_two_references_stay_two() -> None:
    a, b = daya("Agencia Uno"), daya("Agencia Uno")
    assert not daya_merged(a, b, "AU-10234", "AU-10235", flag=True)
    # a shared phone is one advertiser too, whatever the names say
    phone = {"phone": "600 111 222"}
    assert not daya_merged(daya("Agencia Uno", **phone), daya("Agencia Uno SL Costa", **phone), "AU-10234", "AU-10235",
                           flag=True)
