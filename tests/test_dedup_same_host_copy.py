"""Q47: Idealista lists one estate twice (San Juan de Alicante and Urbanova, 14 km apart): the same agency and phone,
price, area and description, only the address differs (the shape of the bench run b4-g4-s5; the description is
rewritten, the agency, URLs and the phone are invented; mocks only)."""

from __future__ import annotations

from dataclasses import replace

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import _same_host_copy, listing_of, same_object
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_runner import FakeMessenger

PHONE = "+34600000000"  # fictitious
IDEALISTA = "https://www.idealista.com/inmueble/{}/"
SAN_JUAN = "Terreno en Nou Nazaret - Frank Espinós, San Juan de Alicante"
URBANOVA = "Terreno en El Palmeral-Urbanova-Tabarca, Alicante / Alacant"
ESTATE = ("Finca de lujo con campo de golf privado de 8 hoyos, casi 500 ha de bosque y explotación agrícola de "
          "cítricos con vistas al mar. Piscina de 900 m², lago privado, a 5 minutos de la playa y con acceso directo "
          "por la autopista. Caminos interiores asfaltados de más de 20 km. Residencia principal, salón de "
          "celebraciones, 2 casas para el servicio, 2 casas para invitados y edificios polivalentes, en total unos "
          "5.000 m² construidos. Usos terciarios permitidos: hotel, restauración, centros deportivos y de ocio. "
          "Sistema de riego con lagos y pozos, almacenes, báscula para camiones y maquinaria agrícola moderna. ")
# a portal post is truncated anywhere (``TEXT_LIMIT``): the second copy loses its last words and one space differs
ESTATE_2 = ESTATE.replace(". Piscina", ".  Piscina")[:-40]


def estate(address: str, **extra) -> dict:
    district = address.removeprefix("Terreno en ").split(",")[0]
    return {"price_amount": 15_000_000, "price_currency": "EUR", "area_m2": 5_000_000.0, "plot_m2": 5_000_000.0,
            "rooms": None, "floor": None, "deal_type": "sale", "property_type": "land", "address": address,
            "district": district, "location": address.removeprefix("Terreno en "), "advertiser": "Agencia Ejemplo",
            "phone": PHONE, **extra}


def post(url: str, description: str, title: str = "Terreno en venta") -> str:
    """A portal post as ``web_search.worker._source_text`` writes it."""
    return f'JSON-LD: {{"url": "{url}", "title": "{title}"}}\n{title}\nСсылка: {url}\n{description}'


def pair(a: dict, b: dict, body_a: str, body_b: str, *, url_a: str = IDEALISTA.format(109103705),
         url_b: str = IDEALISTA.format(110278301), **flags) -> bool:
    la, lb = listing_of(a, url=url_a, body=body_a), listing_of(b, url=url_b, body=body_b)
    return same_object(la, lb, **flags) and same_object(lb, la, **flags)


BENCH = {"without_address": True, "district_not_street": True, "cross_portal_facts": True}  # scripts/sim/harness.py
ON = {**BENCH, "same_host_copy": True}
BODIES = (post(IDEALISTA.format(109103705), ESTATE), post(IDEALISTA.format(110278301), ESTATE_2))


def test_bench_pair_is_one_object_only_with_the_flag() -> None:
    a, b = estate(SAN_JUAN), estate(URBANOVA)
    assert not pair(a, b, *BODIES, **BENCH)  # the bug: two cards
    assert not pair(a, b, *BODIES, district_not_street=True)  # «terreno» is no street token of either address
    assert pair(a, b, *BODIES, **ON)
    assert pair(a, b, *BODIES, district_not_street=True, same_host_copy=True)
    # the agency alone (no phone stated) or the phone alone is the advertiser's sign
    assert pair({**a, "phone": None}, {**b, "phone": None}, *BODIES, **ON)
    assert pair({**a, "advertiser": None}, {**b, "advertiser": None}, *BODIES, **ON)


def test_agency_template_with_other_numbers_is_two_objects() -> None:
    """One agency's template for two flats of one price and area: another floor or door in the text."""
    text = ("Piso exterior en planta {floor}, puerta {door}, con 85 m² útiles, 3 dormitorios y 2 baños, cocina "
            "equipada, salón luminoso con balcón, calefacción central, ascensor y portero físico. Edificio con "
            "fachada rehabilitada, cerca del metro, colegios, mercado y zonas verdes. Gastos de comunidad incluidos "
            "en el precio. Disponible de inmediato, visitas con cita previa en nuestra oficina del barrio. Ideal "
            "como primera vivienda o para inversión, con certificado energético en trámite.")
    flat = {"price_amount": 245_000, "price_currency": "EUR", "area_m2": 85, "rooms": 3, "deal_type": "sale",
            "property_type": "apartment", "advertiser": "Agencia Ejemplo", "phone": PHONE}
    a, b = ({**flat, "address": f"{district}, Valencia", "location": f"{district}, Valencia", "district": district}
            for district in ("Benimaclet", "Ruzafa"))
    body_a = post(IDEALISTA.format(1), text.format(floor=2, door=4))
    body_b = post(IDEALISTA.format(2), text.format(floor=5, door=1))
    assert not pair(a, b, body_a, body_b, url_a=IDEALISTA.format(1), url_b=IDEALISTA.format(2), **ON)
    # the same numbers: one ad filed twice
    assert pair(a, b, body_a, body_a.replace("/1/", "/2/"), url_a=IDEALISTA.format(1), url_b=IDEALISTA.format(2), **ON)


def test_other_facts_or_signs_are_two_objects() -> None:
    a, b = estate(SAN_JUAN), estate(URBANOVA)
    assert not pair(a, {**b, "price_amount": 14_999_000}, *BODIES, **ON)  # the price exactly
    assert not pair(a, {**b, "area_m2": 4_900_000.0, "plot_m2": 4_900_000.0}, *BODIES, **ON)  # area beyond 1 %
    assert not pair(a, {**b, "property_type": "house"}, *BODIES, **ON)
    assert not pair(a, {**b, "property_type": None}, *BODIES, **ON)
    assert not pair(a, {**b, "deal_type": None}, *BODIES, **ON)
    assert not pair(a, {**b, "rooms": 6}, *BODIES, **ON)
    assert not pair({**a, "phone": None, "advertiser": "Agencia Uno"}, {**b, "phone": None, "advertiser": "Agencia Dos"},
                    *BODIES, **ON)  # no shared advertiser
    other = ESTATE.replace("campo de golf privado de 8 hoyos", "bodega propia, olivar y almazara tradicional")
    other = other.replace("lago privado", "pista de tenis").replace("Sistema de riego con lagos y pozos",
                                                                    "Cuadras para caballos y picadero")
    assert not pair(a, b, BODIES[0], post(IDEALISTA.format(110278301), other), **ON)  # Jaccard < 0.9
    short = post(IDEALISTA.format(110278301), ESTATE[:300])
    assert not pair(a, b, post(IDEALISTA.format(109103705), ESTATE[:300]), short, **ON)  # under 400 characters
    # two portals are another rule (``_same_cross_facts``); a social network has its own text rules
    la, lb = listing_of(a, url=IDEALISTA.format(1), body=BODIES[0]), listing_of(b, url=IDEALISTA.format(2), body=BODIES[1])
    assert _same_host_copy(la, lb)
    assert not _same_host_copy(la, replace(lb, url="https://www.fotocasa.es/es/comprar/terreno/alicante/1/d"))
    assert not _same_host_copy(replace(la, url="https://www.facebook.com/groups/1/posts/2"),
                               replace(lb, url="https://www.facebook.com/groups/1/posts/3"))
    assert not _same_host_copy(la, replace(lb, url=la.url))


PLOT = ("Parcela urbana en la urbanización, totalmente llana y con todos los servicios a pie de parcela: agua, luz, "
        "alcantarillado y fibra óptica. Orientación sur, vistas despejadas a la sierra y entorno muy tranquilo, a pocos "
        "minutos del centro del pueblo, colegios y supermercados. Edificabilidad para vivienda unifamiliar aislada con "
        "piscina y jardín. Calles asfaltadas, alumbrado público y zonas verdes comunes. Consulte sin compromiso, "
        "financiación disponible y visitas con cita previa en nuestra oficina.")


def test_typical_plots_of_one_urbanization_are_two_objects() -> None:
    """One agency's typical plots: one price, area and text, one urbanization in the address (the copy rule needs two
    addresses without a shared street or district token)."""
    plot = {"price_amount": 95_000, "price_currency": "EUR", "plot_m2": 800.0, "deal_type": "sale",
            "property_type": "land", "advertiser": "Agencia Ejemplo", "phone": PHONE,
            "address": "Urbanización Los Pinos, Pedrezuela", "district": "Los Pinos", "location": "Los Pinos, Pedrezuela"}
    urls = {"url_a": IDEALISTA.format(1), "url_b": IDEALISTA.format(2)}
    for text in (PLOT, "Parcelas desde 95.000 €. " + PLOT, "Lote de parcelas. " + PLOT):
        bodies = post(IDEALISTA.format(1), text), post(IDEALISTA.format(2), text)
        assert not pair(plot, plot, *bodies, **urls, **ON)
        assert not pair(plot, {**plot, "address": "Urbanización Los Pinos, Pedrezuela, Madrid"}, *bodies, **urls, **ON)


def test_two_named_streets_are_two_objects() -> None:
    """Units of a development not recognised as one: two named streets are two objects."""
    flat = {"price_amount": 210_000, "price_currency": "EUR", "area_m2": 78.0, "rooms": 2, "deal_type": "sale",
            "property_type": "apartment", "advertiser": "Agencia Ejemplo", "phone": PHONE, "location": "Alicante"}
    urls = {"url_a": IDEALISTA.format(1), "url_b": IDEALISTA.format(2)}
    bodies = post(IDEALISTA.format(1), PLOT), post(IDEALISTA.format(2), PLOT)
    a, b = {**flat, "address": "Calle Mayor"}, {**flat, "address": "Avenida Diagonal"}
    assert not pair(a, b, *bodies, **urls, **ON)
    # a street on one side only: the copy rule may apply
    assert pair(a, {**b, "address": "Cabo de las Huertas", "district": "Cabo de las Huertas"}, *bodies, **urls, **ON)


def test_new_development_is_never_merged() -> None:
    a, b = estate(SAN_JUAN), estate(URBANOVA)
    text = "Obra nueva. " + ESTATE
    assert not pair(a, b, post(IDEALISTA.format(109103705), text), post(IDEALISTA.format(110278301), text), **ON)


def test_two_agency_references_are_two_objects() -> None:
    a, b = estate(SAN_JUAN), estate(URBANOVA)

    def ref_post(url: str, ref: str) -> str:
        return f'JSON-LD: {{"url": "{url}", "agency_ref": "{ref}"}}\nTerreno en venta\nСсылка: {url}\n{ESTATE}'

    assert not pair(a, b, ref_post(IDEALISTA.format(109103705), "REF-1001"),
                    ref_post(IDEALISTA.format(110278301), "REF-1002"), **ON)
    assert pair(a, b, ref_post(IDEALISTA.format(109103705), "REF-1001"),
                ref_post(IDEALISTA.format(110278301), "REF-1001"), **ON)


def test_settings_pass_the_flag(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_SAME_HOST_COPY", raising=False)
    assert RunnerConfig().dedup_same_host_copy is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_same_host_copy is False
    monkeypatch.setenv("DEDUP_SAME_HOST_COPY", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_same_host_copy is True


GOAL = "Купить участок в Аликанте до 20000000 евро"


async def _cards(flag: bool) -> tuple[int, dict]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, dedup_without_address=True,
        dedup_district_not_street=True, dedup_cross_portal_facts=True, dedup_same_host_copy=flag))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, code, address, body in (("f1", 109103705, SAN_JUAN, BODIES[0]), ("f2", 110278301, URBANOVA, BODIES[1])):
        link = IDEALISTA.format(code)
        payload = {**estate(address), "original_post_link": link, "summary_ru": "Поместье"}
        store.add_finding(cid, fid, f"🏠 {fid}", payload=payload, original=body, vertical="real_estate", url=link)
    await runner.step(cid)
    return len(messenger.findings()), store.duplicates


async def test_runner_sends_one_card_only_with_the_flag() -> None:
    assert await _cards(False) == (2, {})
    assert await _cards(True) == (1, {"f2": "f1"})
