"""Q67 (``DEDUP_RENT_PLATFORM_COPY``): one rented flat on Fotocasa and Habitaclia through two rent platforms
(HousingAnywhere and Spotahome: other texts, other agency references, no street), and an exact copy of a held seasonal
let. The shape of the bench run b6-g5-s7; the texts are rewritten, the references and URLs are invented; mocks only."""

from __future__ import annotations

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import _same_rent_copy, listing_of, same_object
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


def test_two_platforms_on_two_portals_merge_only_with_the_flag() -> None:
    ha, spot = listing(HA), listing(SPOT)
    assert not same(ha, spot, **BENCH)  # Q15: two other agency references, no street: «another flat»
    assert same(ha, spot)  # the cause: without DEDUP_DISTRICT_NOT_STREET the shared district would do
    assert same(ha, spot, **ON)


def test_the_cluster_of_b6_is_connected() -> None:
    ha, spot, season, rogent = (listing(p) for p in (HA, SPOT, SEASON, ROGENT))
    assert same(spot, season, **BENCH) and same(ha, rogent, **BENCH)  # merged before Q67 already
    assert not same(ha, season, **BENCH) and not same(spot, rogent, **BENCH)  # one portal, two platforms
    assert same(ha, season, **ON) and same(spot, rogent, **ON)


def test_other_facts_are_two_flats() -> None:
    ha = listing(HA)
    for other in (rent(SPOT_URL, "Spotahome", price=1092.0), rent(SPOT_URL, "Spotahome", area=71.0),
                  rent(SPOT_URL, "Spotahome", rooms=3),
                  rent(SPOT_URL, "Spotahome", district="Sant Antoni", location="Sant Antoni, Barcelona"),
                  rent(SPOT_URL, "Spotahome", location="El Camp de l'Arpa del Clot, Badalona"),
                  {**rent(SPOT_URL, "Spotahome"), "deal_type": "sale"},
                  {**rent(SPOT_URL, "Spotahome"), "property_type": "house"}):
        assert not same(ha, listing(other), **ON), other


def test_one_house_two_floors_or_doors() -> None:
    a = rent(HA_URL, "HOUSINGANYWHERE", address="Carrer de Rogent 12, 3º")
    assert same(listing(a), listing(rent(SPOT_URL, "Spotahome", address="Carrer de Rogent 12, 3º")), **ON)
    fourth = listing(rent(SPOT_URL, "Spotahome", address="Carrer de Rogent 12, 4º"))
    assert not _same_rent_copy(listing(a), fourth)  # (the older street rule merges one street and house number)
    assert not same(listing({**HA, "floor": 3}), listing({**SPOT, "floor": 4}), **ON)
    assert not same(listing(a), listing(rent(SPOT_URL, "Spotahome", address="Carrer de Sant Joan 5")), **ON)


def test_one_platforms_units_and_its_template() -> None:
    """One platform's two references are two units (its template text and no street say nothing)."""
    other_unit = BODIES[ROGENT_URL].replace("ut0000042", "ut0000043")
    assert not same(listing(HA), listing(ROGENT, other_unit), **ON)
    same_host = rent(FOTOCASA.format(9005), "HOUSINGANYWHERE")
    assert not same(listing(HA), listing(same_host, post(FOTOCASA.format(9005), TEMPLATE, "ut0000042", "")), **ON)


def test_a_round_rent_needs_a_street_or_the_same_text() -> None:
    ha, spot = rent(HA_URL, "HOUSINGANYWHERE", price=1100.0), rent(SPOT_URL, "Spotahome", price=1100.0)
    assert not same(listing(ha), listing(spot), **ON)  # 1 100 €, 70 m², 2 rooms in one district: common
    long_a, long_b = post(HA_URL, SPOT, "ref001", ""), post(SPOT_URL, SPOT, "ref002", "")
    other = {**spot, "district": "Sant Martí", "location": "Sant Martí, Barcelona"}
    two = listing({**ha, "advertiser": "Agencia Uno"}, long_a), listing({**other, "advertiser": "Dos"}, long_b)
    assert _same_rent_copy(*two) and not same(*two, **BENCH)  # the same long text of two agencies, other districts
    one = listing({**ha, "advertiser": "Uno"}, long_a), listing({**other, "advertiser": "Uno"}, long_b)
    assert not _same_rent_copy(*one)  # one agency, two references: its template for two units
    assert not _same_rent_copy(listing({**ha, "advertiser": "Uno"}, long_a.replace('"ref001"', '"ref002"')),
                               one[1])  # one agency: not this rule (its own references decide, Q15 and Q58)


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


async def test_off_the_copies_of_a_seasonal_let_are_exact_cards(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch)
    for fid, payload in (("s", SEASON), ("h", HA), ("p", SPOT), ("r", ROGENT)):
        add(store, cid, fid, payload)
    await runner.step(cid)
    assert store.buckets["s"][0] == "similar"
    assert len(messenger.findings()) == 2 and store.duplicates == {"r": "h"}  # №6 and №10, as in b6-g5-s7


async def test_on_every_copy_inherits_the_seasonal_let(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch, dedup_rent_platform_copy=True)
    for fid, payload in (("s", SEASON), ("h", HA), ("p", SPOT), ("r", ROGENT)):
        add(store, cid, fid, payload)
    await runner.step(cid)
    assert messenger.findings() == []  # nothing goes out as exact
    assert store.duplicates == {"h": "s", "p": "s", "r": "s"}  # «r» matches the attached «h», not «s» itself
    assert [f.id for f in await store.held_findings(cid, "similar", 10)] == ["s"]
    assert [x["url"] for x in store.cluster_links["s"]] == [HA_URL, SPOT_URL, ROGENT_URL]


async def test_on_two_platform_copies_without_a_seasonal_sign_are_one_card(monkeypatch) -> None:
    store, messenger, runner, cid = await setup(monkeypatch, dedup_rent_platform_copy=True)
    add(store, cid, "h", HA)
    add(store, cid, "p", SPOT)
    await runner.step(cid)
    assert len(messenger.findings()) == 1 and store.duplicates == {"p": "h"}


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
