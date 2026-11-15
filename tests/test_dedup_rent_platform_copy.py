"""Q67 (``DEDUP_RENT_PLATFORM_COPY``): one rented flat on Fotocasa and Habitaclia through two rent platforms
(HousingAnywhere and Spotahome: other texts, other agency references, no street), and an exact copy of a held seasonal
let. The shape of the bench run b6-g5-s7; the texts are rewritten, the references and URLs are invented; mocks only."""

from __future__ import annotations

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import (
    _same_rent_copy,
    compact_agency,
    listing_of,
    rent_platform,
    same_object,
)
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_runner import FakeMessenger

FOTOCASA = "https://www.fotocasa.es/es/alquiler/vivienda/barcelona-capital/amueblado/{}/d"
HABITACLIA = "https://www.habitaclia.com/i{}.htm"
TEMPLATE = ("Este apartamento en Barcelona ofrece un espacio habitable no amueblado perfecto para profesionales o "
            "estudiantes. Disponible para alquilar a través de la plataforma.")
SPOT = ("Descubra este piso de 2 dormitorios en El Camp de l'Arpa del Clot, Barcelona. Totalmente amueblado y equipado "
        "con electrodomésticos modernos, lavavajillas y lavadora, ofrece una estancia cómoda. Disfrute del balcón y de "
        "la calefacción de gas natural. Verificado por la plataforma, lo que garantiza su fiabilidad. Parejas y "
        "estudiantes son bienvenidos a convertirlo en su próximo hogar. El edificio cuenta con ascensor. El barrio es "
        "animado, bien comunicado y con todo tipo de servicios, restaurantes locales y supermercados cerca.")
GOAL = "Снять квартиру в Барселоне до 1500 € в месяц, 2 комнаты"

# every dedup flag the bench runs with (scripts/sim/harness.py)
BENCH = {"without_address": True, "district_not_street": True, "cross_portal_facts": True, "same_host_copy": True,
         "description_normalize": True, "same_lot": True, "twin_listings": True}
ON = {**BENCH, "rent_platform_copy": True}


def post(url: str, description: str, ref: str, address: str) -> str:
    """A portal post as ``web_search.worker._source_text`` writes it (the reference with DEDUP_DISTRICT_NOT_STREET)."""
    return (f'JSON-LD: {{"url": "{url}", "address": "{address}", "agency_ref": "{ref}"}}\nVivienda en alquiler\n'
            f"Ссылка: {url}\n{description}")


def rent(url: str, agency: str, *, price: float = 1091.0, area: float = 70.0, rooms: int = 2,
         district: str = "El Camp de l'Arpa del Clot", location: str = "El Camp de l'Arpa del Clot, Barcelona",
         address: str | None = None, **extra) -> dict:
    return {"price_amount": price, "price_currency": "EUR", "area_m2": area, "rooms": rooms, "floor": None,
            "deal_type": "rent", "property_type": "apartment", "district": district, "location": location,
            "address": address, "advertiser": agency, "original_post_link": url,
            "summary": f"Piso en alquiler en {district}, Barcelona.", **extra}


HA_URL, SPOT_URL, SEASON_URL, ROGENT_URL = (FOTOCASA.format(9001), HABITACLIA.format(9002), FOTOCASA.format(9003),
                                            HABITACLIA.format(9004))
HA = rent(HA_URL, "HOUSINGANYWHERE")  # card №6 of b6-g5-s7
SPOT = rent(SPOT_URL, "Spotahome", location="El Camp de l'Arpa del Clot, Sant Martí, Barcelona")  # card №10
SEASON = rent(SEASON_URL, "Spotahome", rental_term="temporary")  # the same Spotahome ad on Fotocasa, TEMPORARY
ROGENT = rent(ROGENT_URL, "HOUSINGANYWHERE", district="Sant Martí", address="Carrer de Rogent",
              location="Carrer de Rogent, Sant Martí, Barcelona")  # the HousingAnywhere ad on Habitaclia
BODIES = {HA_URL: post(HA_URL, TEMPLATE, "ut0000042", "El Camp de l'Arpa del Clot, Barcelona"),
          SPOT_URL: post(SPOT_URL, SPOT, "4200042", "Sant Martí, Barcelona Capital"),
          SEASON_URL: post(SEASON_URL, SPOT, "4200042", "El Camp de l'Arpa del Clot, Barcelona"),
          ROGENT_URL: post(ROGENT_URL, TEMPLATE, "ut0000042", "Sant Martí, Barcelona Capital")}


def listing(payload: dict, body: str | None = None):
    url = payload["original_post_link"]
    return listing_of(payload, url=url, body=BODIES.get(url, "") if body is None else body)


def same(a, b, **flags) -> bool:
    assert same_object(a, b, **flags) == same_object(b, a, **flags)
    return same_object(a, b, **flags)


def test_flag_off_by_default_and_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_RENT_PLATFORM_COPY", raising=False)
    assert RunnerConfig().dedup_rent_platform_copy is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_rent_platform_copy is False
    monkeypatch.setenv("DEDUP_RENT_PLATFORM_COPY", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_rent_platform_copy is True


def test_a_shared_district_is_no_sign() -> None:
    """№6 and №10 of b6-g5-s7: one district, exact facts, other platforms and references, no street, no link: two
    cards (a false merge is worse); the seasonal copy of №10 joins it by its reference (the runner below)."""
    ha, spot, season = listing(HA), listing(SPOT), listing(SEASON)
    assert not same(ha, spot, **BENCH) and not same(ha, spot, **ON)
    assert same(spot, season, **BENCH)  # one Spotahome reference on two portals: merged before Q67 already
    for district_a, district_b in (("Eixample", "Eixample"), ("Sant Martí", "Sant Antoni")):
        a = rent(HA_URL, "HOUSINGANYWHERE", district=district_a, location=f"{district_a}, Barcelona")
        b = rent(SPOT_URL, "Spotahome", district=district_b, location=f"{district_b}, Barcelona")
        assert not same(listing(a), listing(b), **ON) and not _same_rent_copy(listing(a), listing(b))


def test_the_same_long_text_of_two_advertisers() -> None:
    a, b = long_pair()
    assert _same_rent_copy(a, b) and same(a, b, **ON) and not same(a, b, **BENCH)  # other districts, references
    one = long_pair(agency_b="Agencia Uno")
    assert not _same_rent_copy(*one)  # one advertiser, two references: its template for two units


def long_pair(agency_b: str = "Agencia Dos", **facts):
    a = rent(HA_URL, "Agencia Uno", price=1100.0, district="Fort Pienc", location="Fort Pienc, Barcelona")
    b = rent(SPOT_URL, agency_b, price=1100.0, district="Eixample", location="Eixample, Barcelona", **facts)
    return listing(a, post(HA_URL, SPOT, "ref001", "")), listing(b, post(SPOT_URL, SPOT, "ref002", ""))


def test_other_facts_are_two_flats() -> None:
    for facts in ({"price": 1101.0}, {"area": 71.0}, {"rooms": 3}, {"location": "Eixample, Badalona"},
                  {"deal_type": "sale"}, {"property_type": "house"}):
        a, b = long_pair()
        b = listing({**rent(SPOT_URL, "Agencia Dos", price=1100.0, district="Eixample", location="Eixample, Barcelona"),
                     **{k: v for k, v in facts.items() if k not in ("price", "area", "rooms")},
                     **({"price_amount": facts["price"]} if "price" in facts else {}),
                     **({"area_m2": facts["area"]} if "area" in facts else {}),
                     **({"rooms": facts["rooms"]} if "rooms" in facts else {})}, post(SPOT_URL, SPOT, "ref002", ""))
        assert not _same_rent_copy(a, b), facts


LINK = "https://housinganywhere.com/room/ut0000042/es/Barcelona"


def link_pair(agency_a: str, agency_b: str, ref_b: str = ""):
    a = rent(HA_URL, agency_a, district="Fort Pienc", location="Fort Pienc, Barcelona")
    b = rent(SPOT_URL, agency_b, district="Eixample", location="Eixample, Barcelona")
    body_b = post(SPOT_URL, f"{SPOT}\n{LINK}", "", "")  # another text: only the link is shared
    if ref_b:
        body_b = body_b.replace('"agency_ref": ""', f'"agency_ref": "{ref_b}"')
    return listing(a, post(HA_URL, f"{TEMPLATE}\n{LINK}", "", "")), listing(b, body_b)


def test_the_same_platform_link_and_the_names_of_one_platform() -> None:
    assert compact_agency("Housing Anywhere") == compact_agency("HOUSINGANYWHERE") == compact_agency("Housing-Anywhere")
    assert rent_platform("Housing Anywhere") and rent_platform("spotahome.com") and not rent_platform("Finques Bou")
    a, b = link_pair("HOUSINGANYWHERE", "Housing Anywhere")
    assert same(a, b, **ON) and not same(a, b, **BENCH)  # one link to the platform's ad
    assert not same(*link_pair("HOUSINGANYWHERE", "Housing-Anywhere", ref_b="ut0000043"), **ON)  # one platform, 2 refs
    for agency_a, agency_b in (("", "Housing Anywhere"), ("HOUSINGANYWHERE", "")):
        assert not _same_rent_copy(*link_pair(agency_a, agency_b))  # an unknown advertiser


def test_one_house_two_floors_or_doors() -> None:
    a = listing(rent(HA_URL, "HOUSINGANYWHERE", address="Carrer de Rogent 12, 3º"))
    assert _same_rent_copy(a, listing(rent(SPOT_URL, "Spotahome", address="Carrer de Rogent 12, 3º")))
    for address in ("Carrer de Rogent 12, 4º", "Carrer de Rogent 14, 3º", "Carrer de Rogent", "Carrer de Sant Joan 12, 3º"):
        assert not _same_rent_copy(a, listing(rent(SPOT_URL, "Spotahome", address=address))), address
    assert not same(listing({**HA, "floor": 3}), listing({**SPOT, "floor": 4}), **ON)


def test_one_platforms_units_and_its_template() -> None:
    """One platform's two references are two units (its template text and no street say nothing)."""
    other_unit = BODIES[ROGENT_URL].replace("ut0000042", "ut0000043")
    assert not same(listing(HA), listing(ROGENT, other_unit), **ON)
    same_host = rent(FOTOCASA.format(9005), "HOUSINGANYWHERE")
    assert not same(listing(HA), listing(same_host, post(FOTOCASA.format(9005), TEMPLATE, "ut0000042", "")), **ON)


# -- the runner: a seasonal sign is inherited --


async def setup(monkeypatch, **config):
    monkeypatch.setenv("RENT_DEFAULT_LONG_TERM", "true")  # Q49: a seasonal let is similar (deal_kind)
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, dedup_held_findings=True,
        dedup_district_not_street=True, **config))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)  # the search is still running
    return store, messenger, runner, cid


def add(store: MemoryRunStore, cid: str, fid: str, payload: dict) -> None:
    url = payload["original_post_link"]
    store.add_finding(cid, fid, f"🏠 {fid}", payload=payload, original=BODIES[url], vertical="real_estate", url=url)


B6 = (("s", SEASON), ("h", HA), ("p", SPOT), ("r", ROGENT))


async def test_off_the_copy_of_a_seasonal_let_is_an_exact_card(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch)
    for fid, payload in B6:
        add(store, cid, fid, payload)
    await runner.step(cid)
    assert store.buckets["s"][0] == "similar"
    assert len(messenger.findings()) == 2 and store.duplicates == {"r": "h"}  # №6 and №10, as in b6-g5-s7


async def test_on_the_platform_copy_inherits_the_seasonal_let(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch, dedup_rent_platform_copy=True)
    for fid, payload in B6:
        add(store, cid, fid, payload)
    await runner.step(cid)
    assert len(messenger.findings()) == 1  # №6 stays (no strong sign with the seasonal one); №10 is not exact
    assert store.duplicates == {"p": "s", "r": "h"}  # no chain: «h» and «r» are not attached through «p»
    assert [x["url"] for x in store.cluster_links["s"]] == [SPOT_URL]


IDEALISTA_URL = "https://www.idealista.com/inmueble/9006/"
LONG_TERM = rent(IDEALISTA_URL, "Finques Bou")  # an agency's long lease with the Spotahome reference and text
BODIES[IDEALISTA_URL] = post(IDEALISTA_URL, SPOT, "4200042", "El Camp de l'Arpa del Clot, Barcelona")


async def test_on_a_long_term_copy_of_an_agency_is_not_hidden(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch, dedup_rent_platform_copy=True)
    add(store, cid, "s", SEASON)
    add(store, cid, "i", LONG_TERM)
    await runner.step(cid)
    assert store.duplicates == {} and len(messenger.findings()) == 1  # not a platform, no seasonal sign: exact


async def test_on_a_failure_sends_the_card_as_usual(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch, dedup_rent_platform_copy=True)
    add(store, cid, "s", SEASON)
    await runner.step(cid)

    async def broken(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "attach_to_held", broken)
    add(store, cid, "p", SPOT)
    await runner.step(cid)
    assert len(messenger.findings()) == 1 and store.duplicates == {}


async def test_on_two_platform_copies_with_a_link_are_one_card(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch, dedup_rent_platform_copy=True)
    a, b = (rent(HA_URL, "HOUSINGANYWHERE", district="Fort Pienc", location="Fort Pienc, Barcelona"),
            rent(SPOT_URL, "Housing Anywhere", district="Eixample", location="Eixample, Barcelona"))
    store.add_finding(cid, "a", "🏠 a", payload=a, original=post(HA_URL, f"{TEMPLATE}\n{LINK}", "", ""),
                      vertical="real_estate", url=HA_URL)
    store.add_finding(cid, "b", "🏠 b", payload=b, original=post(SPOT_URL, f"{SPOT}\n{LINK}", "", ""),
                      vertical="real_estate", url=SPOT_URL)
    await runner.step(cid)
    assert len(messenger.findings()) == 1 and store.duplicates == {"b": "a"}


async def test_on_a_seasonal_copy_is_not_hidden_under_a_sent_card(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch, dedup_rent_platform_copy=True)
    add(store, cid, "p", SPOT)
    await runner.step(cid)
    assert len(messenger.findings()) == 1
    add(store, cid, "s", SEASON)  # merged into the exact card without the flag (Q27)
    await runner.step(cid)
    assert "s" not in store.duplicates and store.buckets["s"][0] == "similar"


async def test_off_a_seasonal_copy_joins_the_sent_card(monkeypatch) -> None:
    store, _, runner, cid = await setup(monkeypatch)
    add(store, cid, "p", SPOT)
    await runner.step(cid)
    add(store, cid, "s", SEASON)
    await runner.step(cid)
    assert store.duplicates == {"s": "p"}
