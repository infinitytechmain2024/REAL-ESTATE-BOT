"""Q50: Milanuncios shows an agency's Fotocasa rent ad with «Ref: <reference>.» before the same text, without a street,
phone or agency, under another district name (the shape of the bench run b4-g5-s5; the description is rewritten, the
agency, URLs, reference and the phone are invented; mocks only)."""

from __future__ import annotations

from dataclasses import replace

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import (
    _same_wrapped_description,
    listing_of,
    normalized_description,
    same_object,
)
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_runner import FakeMessenger

PHONE = "+34600000000"  # fictitious
FOTOCASA = "https://www.fotocasa.es/es/alquiler/vivienda/barcelona-capital/ascensor/100000001/d"
MILANUNCIOS = "https://www.milanuncios.com/alquiler-de-pisos-en-barcelona-barcelona/barcelona-capital-600000001.htm"
TEXT = ("Entresuelo de 78 m² reformado por completo, en una zona muy bien comunicada de la ciudad: a un paso de la "
        "estación de autobuses y junto al metro (L1, L2) y al tren de cercanías. Salón comedor con vistas a un patio "
        "interior tranquilo. Con 4 habitaciones es una vivienda pensada para familias. Disponible de inmediato.\n"
        "Solo se atenderán solicitudes con perfil completo y solvencia acreditada.\n\nDistribución:\n"
        "- Salón comedor con vistas al patio\n- Cocina equipada con placa, horno y campana\n"
        "- 4 habitaciones, una con baño propio en una zona independiente\n- 2 baños completos con ducha\n"
        "- Suelo laminado y ventiladores de techo con luz\n\nCondiciones del alquiler:\n"
        "- Renta: 1.314 €/mes\n- Contrato: 7 años\n- Fianza: 1 mensualidad\n- Entrada: inmediata\n\n"
        "Visitas con cita previa, envíe sus documentos de identidad, las últimas nóminas y una carta de presentación "
        "con el número de personas que vivirán en la vivienda. Se valorarán cartas de recomendación de un arrendador "
        "anterior. Toda la información se tratará con absoluta confidencialidad.")
FLAT = {"price_amount": 1314.0, "price_currency": "EUR", "area_m2": 78.0, "rooms": 4, "floor": None,
        "deal_type": "rent", "property_type": "apartment", "features": ["reformado"]}
FC = {**FLAT, "address": "Carrer d'Ausiàs Marc", "district": "Fort Pienc", "location": "Fort Pienc, Barcelona",
      "advertiser": "Administradora Ejemplo", "phone": PHONE}
MI = {**FLAT, "address": None, "district": "Eixample", "location": "Eixample, Barcelona"}


def fotocasa_post(ref: str = "Ausias Marc 127", text: str = TEXT) -> str:
    """A Fotocasa post as ``web_search.worker._source_text`` writes it (a long «JSON-LD» line with the reference)."""
    facts = (f'{{"url": "{FOTOCASA}", "title": "Vivienda en alquiler, Fort Pienc", "price": 1314.0, '
             f'"currency": "EUR", "area_m2": 78.0, "rooms": 4, "address": "Carrer d\'Ausiàs Marc, Fort Pienc, '
             f'Barcelona", "property_type": "vivienda", "deal": "rent", "phone": "{PHONE}", '
             f'"advertiser": "Administradora Ejemplo", "agency_ref": "{ref}"}}')
    return f"JSON-LD: {facts}\nVivienda en alquiler, Fort Pienc\nСсылка: {FOTOCASA}\n{text}"


def milanuncios_post(wrapper: str = "Ref: Ausias Marc 127. ", text: str = TEXT, url: str = MILANUNCIOS) -> str:
    facts = (f'{{"url": "{url}", "title": "Barcelona Capital", "price": 1314.0, "currency": "EUR", "area_m2": 78.0, '
             '"rooms": 4, "address": "Barcelona", "property_type": "apartment", "deal": "rent"}')
    return f"JSON-LD: {facts}\nBarcelona Capital\nСсылка: {url}\n{wrapper}{text} # Parquet #Horno #"


BENCH = {"without_address": True, "district_not_street": True, "cross_portal_facts": True,
         "same_host_copy": True}  # scripts/sim/harness.py before Q50
ON = {**BENCH, "description_normalize": True}


def pair(a: dict, b: dict, body_a: str, body_b: str, *, url_a: str = FOTOCASA, url_b: str = MILANUNCIOS,
         **flags) -> bool:
    la, lb = listing_of(a, url=url_a, body=body_a), listing_of(b, url=url_b, body=body_b)
    return same_object(la, lb, **flags) and same_object(lb, la, **flags)


def test_bench_pair_is_one_object_only_with_the_flag() -> None:
    assert not pair(FC, MI, fotocasa_post(), milanuncios_post(), **BENCH)  # the bug: two cards
    assert pair(FC, MI, fotocasa_post(), milanuncios_post(), **ON)
    assert pair(FC, MI, fotocasa_post(), milanuncios_post(), description_normalize=True)
    # «Referencia:» with the same reference of the agency on the other side
    assert pair(FC, MI, fotocasa_post(ref="AM-127"), milanuncios_post("Referencia: AM-127. "), **ON)


def test_a_reference_on_one_side_only_is_two_objects() -> None:
    """«Ref: AM-12» against a Fotocasa ad without a reference: maybe the agency's template for another unit."""
    assert not pair(FC, MI, fotocasa_post(ref=""), milanuncios_post("Referencia: AM-127. "), **ON)
    assert not pair(FC, MI, fotocasa_post(ref=""), milanuncios_post("Ref: AM-12. "), **ON)
    # a weak reference (a year) on both sides is no sign of this rule (it adds no small number: the old rules decide)
    la = listing_of(FC, url=FOTOCASA, body=fotocasa_post(ref="2024"))
    assert not _same_wrapped_description(la, listing_of(MI, url=MILANUNCIOS, body=milanuncios_post("Ref: 2024. ")))


def test_normalized_description() -> None:
    assert normalized_description("Ref: REO-111-860. Piso ocupado.") == ("Piso ocupado.", "reo111860")
    assert normalized_description("Piso ocupado. Ref: 12.") == ("Piso ocupado. Ref: 12.", "")
    assert normalized_description("") == ("", "")


def test_two_references_are_two_objects() -> None:
    assert not pair(FC, MI, fotocasa_post(), milanuncios_post("Ref: Ausias Marc 129. "), **ON)
    assert not pair(FC, MI, fotocasa_post(ref="AM-1"), milanuncios_post("Ref: AM-2. "), **ON)


def test_the_wrapper_is_the_cause() -> None:
    """The same posts without «Ref: ...» already merge by the old text rule: the wrapper's number broke it."""
    assert pair(FC, MI, fotocasa_post(), milanuncios_post(""), **BENCH)
    assert not pair(FC, MI, fotocasa_post(), milanuncios_post(), **BENCH)


def test_other_facts_are_two_objects() -> None:
    body_a, body_b = fotocasa_post(), milanuncios_post()
    assert not pair(FC, {**MI, "price_amount": 1300.0}, body_a, body_b, **ON)
    assert not pair(FC, {**MI, "area_m2": 70.0}, body_a, body_b, **ON)
    assert not pair(FC, {**MI, "area_m2": None}, body_a, body_b, **ON)
    assert not pair(FC, {**MI, "rooms": None}, body_a, body_b, **ON)
    assert not pair(FC, {**MI, "property_type": "house"}, body_a, body_b, **ON)
    assert not pair(FC, {**MI, "property_type": None}, body_a, body_b, **ON)
    assert not pair(FC, {**MI, "deal_type": None}, body_a, body_b, **ON)
    assert not pair(FC, {**MI, "location": "Eixample, Madrid", "district": "Eixample"}, body_a, body_b, **ON)
    assert not pair(FC, {**MI, "address": "Carrer de Mallorca"}, body_a, body_b, **ON)  # two named streets
    other = TEXT.replace("Entresuelo de 78 m²", "Ático de 78 m²").replace("patio interior tranquilo",
                                                                          "terraza con vistas al mar")
    other = other.replace("Suelo laminado y ventiladores de techo", "Suelo de parquet y aire acondicionado")
    assert not pair(FC, MI, body_a, milanuncios_post(text=other), **ON)  # Jaccard < 0.9
    assert not pair(FC, MI, fotocasa_post(text=TEXT[:300]), milanuncios_post(text=TEXT[:300]), **ON)  # < 400 chars
    template = TEXT.replace("2 baños", "3 baños")
    assert not pair(FC, MI, body_a, milanuncios_post(text=template), **ON)  # other small numbers
    news = "Obra nueva. " + TEXT
    assert not pair(FC, MI, fotocasa_post(text=news), milanuncios_post(text=news), **ON)


def test_one_host_or_a_social_network_is_another_rule() -> None:
    la = listing_of(FC, url=FOTOCASA, body=fotocasa_post())
    lb = listing_of(MI, url=MILANUNCIOS, body=milanuncios_post())
    assert _same_wrapped_description(la, lb)
    assert not _same_wrapped_description(la, replace(lb, url=FOTOCASA.replace("100000001", "100000002")))
    assert not _same_wrapped_description(la, replace(lb, url="https://www.facebook.com/groups/1/posts/2"))


def test_two_aggregators_of_one_flat_stay_two_cards() -> None:
    """Spotahome and HousingAnywhere rent one flat through Fotocasa and Habitaclia: other texts, other references
    (b4-g5-s5 №11/№14, №10/№20)."""
    room = {"price_amount": 1091.0, "price_currency": "EUR", "area_m2": 70.0, "rooms": 2, "deal_type": "rent",
            "property_type": "apartment", "address": None, "district": "El Camp de l'Arpa del Clot",
            "location": "El Camp de l'Arpa del Clot, Barcelona"}
    spot = ("Descubra este piso de 2 dormitorios totalmente amueblado y equipado con electrodomésticos modernos, "
            "lavavajillas y lavadora. Balcón, calefacción de gas natural y ascensor. " * 4)
    anywhere = ("Este apartamento ofrece un espacio habitable perfecto para profesionales o estudiantes, disponible "
                "para alquilar a través de la plataforma. " * 4)
    habitaclia = "https://www.habitaclia.com/i100000000001.htm"

    def post(url: str, ref: str, text: str) -> str:
        return f'JSON-LD: {{"url": "{url}", "agency_ref": "{ref}"}}\nPiso en alquiler\nСсылка: {url}\n{text}'

    a, b = {**room, "advertiser": "Spotahome"}, {**room, "advertiser": "HousingAnywhere"}
    assert not pair(a, b, post(FOTOCASA, "1750755", spot), post(habitaclia, "ut1751642", anywhere),
                    url_b=habitaclia, **ON)
    # the same text under a wrapper does not help two references either
    assert not pair(a, b, post(FOTOCASA, "1750755", spot), post(habitaclia, "ut1751642", "Ref: 1750755. " + spot),
                    url_b=habitaclia, **ON)


def test_settings_pass_the_flag(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_DESCRIPTION_NORMALIZE", raising=False)
    assert RunnerConfig().dedup_description_normalize is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_description_normalize is False
    monkeypatch.setenv("DEDUP_DESCRIPTION_NORMALIZE", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_description_normalize is True


GOAL = "Снять квартиру в Барселоне до 1500 евро"


async def _cards(flag: bool) -> tuple[int, dict]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, dedup_without_address=True,
        dedup_district_not_street=True, dedup_cross_portal_facts=True, dedup_same_host_copy=True,
        dedup_description_normalize=flag))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, link, payload, body in (("f1", FOTOCASA, FC, fotocasa_post()), ("f2", MILANUNCIOS, MI, milanuncios_post())):
        store.add_finding(cid, fid, f"🏠 {fid}", payload={**payload, "original_post_link": link, "summary_ru": "Квартира"},
                          original=body, vertical="real_estate", url=link)
    await runner.step(cid)
    return len(messenger.findings()), store.duplicates


async def test_runner_sends_one_card_only_with_the_flag() -> None:
    assert await _cards(False) == (2, {})
    assert await _cards(True) == (1, {"f2": "f1"})
