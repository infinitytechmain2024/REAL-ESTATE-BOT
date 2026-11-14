"""Q55 (``HELD_OFFER_QUALITY``): after «Одобрить» no listing the reviewer failed is sent, and one lot listed twice is one
card (the shape of the bench run b5-g1-s6; descriptions rewritten, agencies, phones, URLs and cadastral references
invented; mocks only)."""

from __future__ import annotations

from bot.campaign.dedup import listing_of, same_object
from bot.campaign.offers import MemoryOffer
from bot.campaign.relevance import Relevance, review_match
from bot.campaign.runner import RunnerConfig
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.tolerance import Match
from tests.test_campaign_relevance import FakeJudge, plot, setup

BENCH = {"without_address": True, "district_not_street": True, "cross_portal_facts": True, "same_host_copy": True,
         "description_normalize": True}  # scripts/sim/harness.py
ON = {**BENCH, "same_lot": True}
IDEALISTA = "https://www.idealista.com/inmueble/{}/"

# The reviewer on a 1 500 m² plot for «от 2000 м²»: the area fails, the listing is rejected.
AREA_FAIL = Relevance("reject", "Площадь меньше нужной.", review={"overall": "reject", "criteria": [
    {"name": "place", "verdict": "pass"}, {"name": "deal", "verdict": "pass"}, {"name": "type", "verdict": "pass"},
    {"name": "area", "verdict": "fail", "quote": "solar de 1500 metros"}]}, model="fake")


def test_flag_off_by_default_and_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("HELD_OFFER_QUALITY", raising=False)
    assert RunnerConfig().held_offer_quality is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().held_offer_quality is False
    monkeypatch.setenv("HELD_OFFER_QUALITY", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().held_offer_quality is True


def test_review_match_excludes_a_failed_measured_number_only_with_the_flag() -> None:
    rules = Match("similar", 0.25, "area", 1500.0)
    assert review_match(rules, AREA_FAIL.review) == rules  # as before: the near-match question is kept
    strict = review_match(rules, AREA_FAIL.review, strict_fail=True)
    assert (strict.bucket, strict.why) == ("excluded", "area")
    budget = {"overall": "reject", "criteria": [{"name": "budget", "verdict": "fail"}]}
    assert review_match(Match("similar", 0.2, "price"), budget, strict_fail=True).bucket == "excluded"
    unknown = {"overall": "near", "criteria": [{"name": "area", "verdict": "unknown"}]}
    assert review_match(Match("exact"), unknown, strict_fail=True).bucket == "similar"  # unknown is still offered


def test_strict_keeps_an_approved_deviation_it_covers() -> None:
    """The person approved a smaller area in the interview: the reviewer's area fail is covered, not a fail."""
    approved = Match("exact", 0.1, "approved_deviation", note="площадь 1 700 м² при запросе от 2 000 м²",
                     covers=("area",))
    kept = review_match(approved, AREA_FAIL.review, strict_fail=True)
    assert (kept.bucket, kept.why) == ("exact", "approved_deviation")
    budget = {"overall": "reject", "criteria": [{"name": "area", "verdict": "fail"}, {"name": "budget", "verdict": "fail"}]}
    assert review_match(approved, budget, strict_fail=True).bucket == "excluded"  # a fail it does not cover


async def _rejected_plot(flag: bool):
    _, store, messenger, runner, cid = await setup(FakeJudge(AREA_FAIL), held_offer_quality=flag)
    store.add_finding(cid, "small", "🏠 small", payload=plot(1500), original="Vendo solar de 1500 metros",
                      vertical="real_estate")
    await runner.tick()
    if (cid, "similar") in store.desk.offers:
        store.desk.offers[(cid, "similar")].state = "approved"
    await runner.tick()
    return store, messenger


async def test_a_rejected_plot_is_not_offered_nor_sent_after_approval() -> None:
    store, messenger = await _rejected_plot(True)
    assert store.buckets["small"][0] == "excluded"
    assert messenger.asks == [] and messenger.findings() == []


async def test_flag_off_the_rejected_plot_is_offered_and_sent_as_before() -> None:
    store, messenger = await _rejected_plot(False)
    assert store.buckets["small"][0] == "similar" and len(messenger.asks) == 1
    assert len(messenger.findings()) == 1


LOT = ("Suelos rústicos desde 29,820 m² ubicados en Villanueva del Ejemplo, Madrid. Se trata de 7 suelos no "
       "urbanizables, formando dos lotes, uno de ellos compuesto de 5 suelos con una superficie total de 485275 m² y el "
       "segundo lote formado por 2 suelos con una superficie de 256822 m², de carácter rústico y uso agropecuario. Los "
       "suelos se sitúan en el perímetro rural del municipio, con buenos accesos por la carretera comarcal. En el "
       "pueblo hay supermercados, restaurantes y otros negocios que atienden las necesidades básicas de los vecinos. "
       "Con nuestros servicios podrá encontrar el suelo rústico que necesita. Empiece ahora pidiendo información.")
PARCELS = ("Lote de 31 fincas de suelo no urbanizable de especial protección. Suman 785,000 m² de carácter rústico y uso "
           "agropecuario y se corresponden parcialmente con dos referencias catastrales, 28999A001000010000XX y "
           "28999A001000020000XY. Se sitúa en el paraje Ejemplo, en la periferia del municipio.")


def post(url: str, description: str) -> str:
    """A portal post as ``web_search.worker._source_text`` writes it."""
    return f'JSON-LD: {{"url": "{url}", "title": "Terreno en venta"}}\nTerreno en venta\nСсылка: {url}\n{description}'


def lot(address: str, agency: str, phone: str, **extra) -> dict:
    return {"price_amount": 694_000, "price_currency": "EUR", "area_m2": None, "rooms": None, "floor": None,
            "deal_type": "sale", "property_type": "land", "address": address, "advertiser": agency, "phone": phone,
            "location": f"{address}, Villanueva del Ejemplo, Madrid", "country": "ES", **extra}


def pair(a: dict, b: dict, body_a: str, body_b: str, url_a: str, url_b: str, **flags) -> bool:
    la, lb = listing_of(a, url=url_a, body=body_a), listing_of(b, url=url_b, body=body_b)
    return same_object(la, lb, **flags) and same_object(lb, la, **flags)


URLS = (IDEALISTA.format(1001), IDEALISTA.format(1002))
BODIES = (post(URLS[0], LOT), post(URLS[1], LOT + " Un responsable le atenderá personalmente."))
A = lot("Calle Ejemplo, 5", "Agencia Uno", "+34600000001")
B = lot("Calle Ejemplo", "Agencia Dos", "+34600000002")


def test_one_portal_lot_at_one_address_with_one_text_and_price_is_one_object() -> None:
    assert not pair(A, B, *BODIES, *URLS, **BENCH)  # the bug: the rule of one host needs the floor or the area
    assert pair(A, B, *BODIES, *URLS, **ON)


def test_the_lot_rule_stays_narrow() -> None:
    assert not pair(A, {**B, "price_amount": 690_000}, *BODIES, *URLS, **ON)  # the price exactly
    assert not pair(A, {**B, "address": "Camino Otro"}, *BODIES, *URLS, **ON)  # another street
    flat = {"property_type": "apartment", "deal_type": "rent", "price_amount": 1485}
    assert not pair({**A, **flat}, {**B, **flat}, *BODIES, *URLS, **ON)  # an agency template of flats
    other = post(URLS[1], LOT.replace("7 suelos", "9 suelos"))
    assert not pair(A, B, BODIES[0], other, *URLS, **ON)  # other numbers in the text
    assert not pair(A, B, post(URLS[0], "Obra nueva. " + LOT), post(URLS[1], "Obra nueva. " + LOT), *URLS, **ON)


def test_the_lot_rule_needs_the_plot_area_unknown_on_both_sides() -> None:
    assert not pair({**A, "plot_m2": 500.0}, {**B, "plot_m2": 1200.0}, *BODIES, *URLS, **ON)
    assert not pair({**A, "plot_m2": 900.0}, B, *BODIES, *URLS, **ON)
    assert not pair({**A, "area_m2": 900.0}, B, *BODIES, *URLS, **ON)


NEIGHBOURS = ("Parcela rústica de 5.000 m² con referencia catastral 28999A001000010000XX, llana y con acceso por camino "
              "asfaltado. Linda con la parcela 28999A001000020000XY, también en venta por la misma propiedad.")


def test_a_neighbour_named_in_the_text_is_no_reference_of_the_plot() -> None:
    a = lot("Camino del Paraje", "Agencia Uno", "+34600000001", price_amount=60_000, area_m2=5_000.0, plot_m2=5_000.0)
    b = {**a, "price_amount": 65_000}
    urls = (IDEALISTA.format(3001), IDEALISTA.format(3002))
    second = NEIGHBOURS.replace("referencia catastral 28999A001000010000XX", "referencia catastral 28999A001000020000XY")
    second = second.replace("Linda con la parcela 28999A001000020000XY", "Linda con la parcela 28999A001000010000XX")
    la = listing_of(a, url=urls[0], body=post(urls[0], NEIGHBOURS))
    assert la.parcels == {"28999A001000010000XX"}
    assert not pair(a, b, post(urls[0], NEIGHBOURS), post(urls[1], second), *urls, **ON)


def test_one_plot_with_the_same_cadastral_references_at_two_prices_is_one_object() -> None:
    a = lot("Camino del Paraje", "Agencia Uno", "+34600000001", price_amount=357_900, area_m2=785_000.0,
            plot_m2=785_000.0, location="Camino del Paraje, Pueblo Uno, Madrid")
    b = lot("Camino del Paraje s/n", "Agencia Dos", "+34600000002", price_amount=396_400, area_m2=785_000.0,
            plot_m2=785_000.0, location="Camino del Paraje, Pueblo Dos, Madrid")
    urls = (IDEALISTA.format(2001), IDEALISTA.format(2002))
    bodies = (post(urls[0], "Suelo rústico en Madrid. " + PARCELS), post(urls[1], PARCELS))
    assert not pair(a, b, *bodies, *urls, **BENCH)  # the bug: the prices differ by 10 %
    assert pair(a, b, *bodies, *urls, **ON)
    one_ref = post(urls[1], PARCELS.replace(" y 28999A001000020000XY", ""))
    assert not pair(a, b, bodies[0], one_ref, *urls, **ON)  # another set of parcels
    assert not pair(a, {**b, "area_m2": 700_000.0, "plot_m2": 700_000.0}, *bodies, *urls, **ON)  # part of the lot
    assert not pair(a, {**b, "property_type": "house"}, *bodies, *urls, **ON)


async def _approved_cards(flag: bool) -> tuple[int, dict]:
    """Three portal ads of one lot, held as similar (the area is unknown for «от 2000 м²»), approved at once."""
    _, store, messenger, runner, cid = await setup(None, held_offer_quality=flag)
    for fid, code, payload, text in (("l1", 1001, A, LOT), ("l2", 1002, B, LOT),
                                     ("l3", 1003, lot("Calle Ejemplo", "Agencia Tres", "+34600000003"), LOT)):
        url = IDEALISTA.format(code)
        store.add_finding(cid, fid, f"🏠 {fid}", payload={**payload, "original_post_link": url}, original=post(url, text),
                          vertical="real_estate", url=url)
    store.desk.offers[(cid, "similar")] = MemoryOffer(42, "approved")
    await runner.tick()
    await runner.tick()
    return len(messenger.findings()), store.duplicates


async def test_copies_among_the_approved_are_one_card_only_with_the_flag() -> None:
    assert await _approved_cards(False) == (3, {})
    assert await _approved_cards(True) == (1, {"l2": "l1", "l3": "l1"})
