"""Q58 (``DEDUP_TWIN_LISTINGS``): one object listed twice with one long description, price and area: a Fotocasa repost
(agency references «X18484» and «X18484R») and an Idealista plot under two municipalities by two offices of one
servicer (two phones). The shape of the bench run b5-g4-s6; the texts are rewritten, the references, phones, agencies
and URLs are invented; mocks only."""

from __future__ import annotations

from dataclasses import replace

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.dedup import _same_twin, listing_of, same_object
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_runner import FakeMessenger

FOTOCASA = "https://www.fotocasa.es/es/comprar/terreno/aspe/pedanias-este/{}/d"
IDEALISTA = "https://www.idealista.com/inmueble/{}/"
RUSTIC = ("Oportunidad en la zona\n\nAmplia parcela rústica en muy buena zona del municipio. Perfectamente comunicada "
          "y cerca de las vías principales, se halla muy cerca del núcleo urbano y de zonas residenciales. La parcela, "
          "perfectamente delimitada, con puerta y vallado perimetral, dispone de acometidas cercanas de agua y luz. "
          "Concierte ahora una visita y compruebe personalmente la oportunidad que supone la adquisición de esta "
          "propiedad, ya sea como inversión, dada su magnífica ubicación, o para su explotación. La presente "
          "publicación tiene carácter meramente informativo, orientativo y no vinculante, por lo que no supone "
          "información contractual alguna. Nota informativa conforme al Art. 20 TRLGDCU.")
SHARE = ("Suelo no urbanizable con una superficie de 665.626 m² situado en la localidad, provincia de Alicante. El suelo "
         "se localiza a 6 km del casco urbano, comunicado mediante caminos y el acceso viario CV-770. La zona dispone "
         "de equipamiento básico a 11 minutos, con restaurantes, colegio y parque. Con nuestros servicios podrá "
         "conocer las posibilidades reales de este suelo y valorar sus posibilidades de inversión. La parte vendedora "
         "es propietaria de una participación indivisa del inmueble, no siendo por ello propietaria del 100 en pleno "
         "dominio. El inmueble se transmitirá como cuerpo cierto y a tanto alzado, en el estado registral y catastral "
         "en el que se encuentre.")


def post(url: str, description: str, ref: str | None = None, title: str = "Terreno en venta") -> str:
    """A portal post as ``web_search.worker._source_text`` writes it (the reference with DEDUP_DISTRICT_NOT_STREET)."""
    facts = f'"url": "{url}"' + (f', "agency_ref": "{ref}"' if ref else "")
    return f"JSON-LD: {{{facts}}}\n{title}\nСсылка: {url}\n{description}"


def plot(**extra) -> dict:
    return {"price_amount": 90_000, "price_currency": "EUR", "area_m2": 10_500.0, "plot_m2": 10_500.0, "rooms": None,
            "floor": None, "deal_type": "sale", "property_type": "land", "district": "Pedanías Este",
            "location": "Pedanías Este, Aspe, Alicante", "advertiser": "Agencia Ejemplo", "phone": "+34600000001",
            **extra}


def share(address: str, phone: str, agency: str, **extra) -> dict:
    return {"price_amount": 15_600, "price_currency": "EUR", "area_m2": 665_626.0, "plot_m2": 665_626.0,
            "rooms": None, "floor": None, "deal_type": "sale", "property_type": "land", "address": address,
            "location": address, "advertiser": agency, "phone": phone, **extra}


ORXETA = share("Via Sense Nom, Orxeta", "+34600000002", "Servicer Uno")
SELLA = share("Camino de l'Ermita, Sella", "+34600000003", "Servicer Dos")

# every dedup flag the bench runs with (scripts/sim/harness.py)
BENCH = {"without_address": True, "district_not_street": True, "cross_portal_facts": True, "same_host_copy": True,
         "description_normalize": True, "same_lot": True}
ON = {**BENCH, "twin_listings": True}


def pair(a: dict, b: dict, body_a: str, body_b: str, url_a: str, url_b: str, **flags) -> bool:
    la, lb = listing_of(a, url=url_a, body=body_a), listing_of(b, url=url_b, body=body_b)
    return same_object(la, lb, **flags) and same_object(lb, la, **flags)


def repost(ref_a: str = "X18484", ref_b: str = "X18484R", text_a: str = RUSTIC, text_b: str = RUSTIC,
           a: dict | None = None, b: dict | None = None, **flags) -> bool:
    url_a, url_b = FOTOCASA.format(191000014), FOTOCASA.format(191000024)
    return pair(a or plot(), b or plot(), post(url_a, f"Referencia {ref_a}\n{text_a}", ref_a),
                post(url_b, f"Referencia {ref_b}\n{text_b}", ref_b), url_a, url_b, **flags)


def two_towns(a: dict = ORXETA, b: dict = SELLA, text_a: str = SHARE, text_b: str = SHARE, **flags) -> bool:
    url_a, url_b = IDEALISTA.format(106000051), IDEALISTA.format(110000005)
    return pair(a, b, post(url_a, text_a, title=f"Terreno en {a['address']}"),
                post(url_b, text_b, title=f"Terreno en {b['address']}"), url_a, url_b, **flags)


def test_bench_pairs_are_one_object_only_with_the_flag() -> None:
    assert not repost(**BENCH)  # the bug: the references differ (Q15), one portal never merges by text
    assert not two_towns(**BENCH)  # the bug: two named streets, no shared phone or agency (Q47)
    assert repost(**ON)
    assert two_towns(**ON)
    assert repost(twin_listings=True) and two_towns(twin_listings=True)  # the rule needs no other flag
    assert repost("X18484", "X18484", **ON)  # one reference: the same ad
    assert repost(**ON, a=plot(address="Partida Uno"), b=plot(address="Partida Dos"))


def test_other_price_or_area_is_two_objects() -> None:
    assert not repost(**ON, b=plot(price_amount=90_001))
    assert not repost(**ON, b=plot(area_m2=10_550.0, plot_m2=10_550.0))
    assert not two_towns(b={**SELLA, "price_amount": 15_500}, **ON)
    assert not two_towns(b={**SELLA, "area_m2": 665_000.0, "plot_m2": 665_000.0}, **ON)  # within 1 %, not equal
    assert not two_towns(b={**SELLA, "price_amount": None}, **ON)
    assert not two_towns(a={**ORXETA, "area_m2": None, "plot_m2": None},
                         b={**SELLA, "area_m2": None, "plot_m2": None}, **ON)


def test_short_template_or_other_text_is_two_objects() -> None:
    short = SHARE[:300]
    assert not two_towns(text_a=short, text_b=short, **ON)  # under 400 characters
    other = SHARE.replace("6 km", "9 km")  # other small numbers: another plot of a template
    assert not two_towns(text_b=other, **ON)
    rewritten = (SHARE.replace("comunicado mediante caminos", "con acceso por pista forestal")
                 .replace("restaurantes, colegio y parque", "comercios, farmacia y centro de salud")
                 .replace("cuerpo cierto y a tanto alzado", "unidad indivisible sin reservas"))
    assert not two_towns(text_b=rewritten, **ON)  # Jaccard < 0.9


def test_other_references_or_template_plots_are_two_objects() -> None:
    assert not repost("X18484", "X18485", **ON)  # another unit of the agency
    assert not repost("X18484", "X18484B", **ON)  # only a repost suffix
    # a round area (typical plots of one estate under one template text) needs the references on both sides
    url_a, url_b = FOTOCASA.format(191000014), FOTOCASA.format(191000024)
    bare = post(url_a, RUSTIC), post(url_b, RUSTIC)
    assert not pair(plot(), plot(), *bare, url_a, url_b, **ON)
    assert not pair(plot(), plot(), post(url_a, RUSTIC, "X18484"), bare[1], url_a, url_b, **ON)
    cadastre = RUSTIC + " Referencia catastral 03019A00500123."
    assert pair(plot(), plot(), post(url_a, cadastre), post(url_b, cadastre), url_a, url_b, **ON)
    # an area that is not round: one exact area, price and long text are enough (the references are unknown)
    assert two_towns(**ON)


def test_type_deal_rooms_new_build_and_social_are_two_objects() -> None:
    assert not two_towns(b={**SELLA, "property_type": "house"}, **ON)
    assert not two_towns(b={**SELLA, "property_type": None}, **ON)
    assert not two_towns(b={**SELLA, "deal_type": None}, **ON)
    assert not two_towns(b={**SELLA, "rooms": 3}, **ON)
    assert not two_towns(text_a="Obra nueva. " + SHARE, text_b="Obra nueva. " + SHARE, **ON)
    la = listing_of(ORXETA, url=IDEALISTA.format(1), body=post(IDEALISTA.format(1), SHARE))
    lb = listing_of(SELLA, url=IDEALISTA.format(2), body=post(IDEALISTA.format(2), SHARE))
    assert _same_twin(la, lb)
    assert _same_twin(la, replace(lb, url="https://www.fotocasa.es/es/comprar/terreno/sella/1/d"))  # two portals
    assert not _same_twin(replace(la, url="https://www.facebook.com/groups/1/posts/2"),
                          replace(lb, url="https://www.facebook.com/groups/1/posts/3"))
    assert not _same_twin(la, replace(lb, url=la.url))


def test_settings_pass_the_flag(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_TWIN_LISTINGS", raising=False)
    assert RunnerConfig().dedup_twin_listings is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_twin_listings is False
    monkeypatch.setenv("DEDUP_TWIN_LISTINGS", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().dedup_twin_listings is True


GOAL = "Купить участок в Аликанте до 300000 евро"


async def _cards(flag: bool) -> tuple[int, dict]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, dedup_without_address=True,
        dedup_district_not_street=True, dedup_cross_portal_facts=True, dedup_same_host_copy=True,
        dedup_description_normalize=True, held_offer_quality=True, dedup_twin_listings=flag))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, code, payload in (("f1", 106000051, ORXETA), ("f2", 110000005, SELLA)):
        link = IDEALISTA.format(code)
        body = post(link, SHARE, title=f"Terreno en {payload['address']}")
        store.add_finding(cid, fid, f"🏠 {fid}", payload={**payload, "original_post_link": link, "summary_ru": "Участок"},
                          original=body, vertical="real_estate", url=link)
    await runner.step(cid)
    return len(messenger.findings()), store.duplicates


async def test_runner_sends_one_card_only_with_the_flag() -> None:
    assert await _cards(False) == (2, {})
    assert await _cards(True) == (1, {"f2": "f1"})


def test_a_short_template_prefix_of_a_long_text_is_two_objects() -> None:
    """The common length hides the rest of the longer text: the lengths must be within ``TWIN_LENGTH_RATIO``."""
    tail = (" Además la finca cuenta con una nave agrícola de reciente construcción, balsa de riego propia, pozo "
            "con concesión de agua, arbolado de almendros y olivos en plena producción y un camino interior que "
            "recorre toda la propiedad de norte a sur.")
    assert not two_towns(text_b=SHARE + tail + tail, **ON)
    assert not two_towns(text_a=SHARE + tail + tail, **ON)
    assert two_towns(text_b=SHARE + " Consulte condiciones.", **ON)  # a few words more: one text


def test_round_area_is_checked_on_the_rounded_area_both_ways() -> None:
    """1000.0 and 1000.4 m² are one area (rounded) and both round: without references or a cadastre two objects."""
    url_a, url_b = FOTOCASA.format(191000014), FOTOCASA.format(191000024)
    bare = post(url_a, RUSTIC), post(url_b, RUSTIC)
    exact, close = plot(area_m2=1000.0, plot_m2=1000.0), plot(area_m2=1000.4, plot_m2=1000.4)
    assert not pair(exact, close, *bare, url_a, url_b, **ON)
    assert not pair(close, exact, *bare[::-1], url_b, url_a, **ON)
    la, lb = listing_of(exact, url=url_a, body=bare[0]), listing_of(close, url=url_b, body=bare[1])
    assert not _same_twin(la, lb) and not _same_twin(lb, la)
    # with the references on both sides the round area merges, in both directions
    refs = post(url_a, RUSTIC, "X18484"), post(url_b, RUSTIC, "X18484R")
    la, lb = listing_of(exact, url=url_a, body=refs[0]), listing_of(close, url=url_b, body=refs[1])
    assert _same_twin(la, lb) and _same_twin(lb, la)
