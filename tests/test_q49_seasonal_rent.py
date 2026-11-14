"""Q49: a seasonal, temporary or holiday let is a similar variant for a home rent that asked for no short term
(RENT_DEFAULT_LONG_TERM, off by default). Sim b4-g5-s5: «снять piso в Барселоне до 1500», the term answered «не знаю
пока», 13 seasonal lets went out as exact. Mocks only, no network.
"""

from __future__ import annotations

import hashlib
import itertools

import pytest

from bot.agents import reviewer as rv
from bot.analysis_pipeline.formatters import source_line_facts
from bot.campaign import final_report
from bot.campaign.relevance import ReviewerJudge
from bot.campaign.tolerance import (
    SEASONAL_NOTE,
    Request,
    classify,
    rent_default_long_enabled,
    request_for,
)
from bot.utils.listing_text import seasonal_let_named
from bot.web_search.sources.portals import ApifyFotocasaSource, ApifyIdealistaPortalSource
from tests.test_portal_rental_term import (
    RENT,
    SOURCE_TEXT,
    TERM_QUESTION,
    fotocasa_row,
    idealista_row,
    text_of,
)
from tests.test_reviewer import settings

DONT_KNOW = {"context": {"answers": [{"question": TERM_QUESTION, "answer": "не знаю пока"}]}}
SEASON = {"context": {"answers": [{"question": TERM_QUESTION, "answer": "на сезон"}]}}
FLAT = {"deal_type": "rent", "property_type": "apartment", "price_amount": 1200, "price_currency": "EUR",
        "location": "El Raval, Barcelona"}

# sha256 of every reviewer prompt before Q49 (main at a7cf6ae), by (soft_wishes, townhouse_is_house, quote_hits,
# non_residential, tenant): the flag off sends them byte for byte.
OLD_SHA256 = {
    (False, False, False, False, False): "dcfe0a2bc6d951aaa6eaf74a16fb8f93ce3ac8436f7f9d2319f2c35a661b1a51",
    (False, False, False, False, True): "3af25e8b0102722f3a19c45e84e288d10683bf81bb6f40e61029da0428d1694f",
    (False, False, False, True, False): "210780ecba9cfcffdcd453b558a999f916b330c90213e524a4ebcde3b55edaeb",
    (False, False, False, True, True): "77a55458d8da26aaa5ce9c6e387be4e7690f0ecd01bdad866b5ff3a774fe856c",
    (False, False, True, False, False): "c3a7097a7331dab40f88820ea609369ee4f5619c7f6e5dc4f21824fe2e98c109",
    (False, False, True, False, True): "f8098272de0741f965ff570053eff421c07a630fe12987341f55d8513a0cf8d7",
    (False, False, True, True, False): "66a7461e1040c3eb87e4de783f0a02a4d2e15d8e2dcad30decc1faf3f7fcf573",
    (False, False, True, True, True): "ce782b2986809687dd95140a0f9c356a407083970b45cfe8d0a37d15e6214346",
    (False, True, False, False, False): "91cd5248734674d6be3b939dbc80ce29c29c3d68756a0ae9314def21676adcc3",
    (False, True, False, False, True): "67b3e628d211ec46de443d72bb64122fe4c3e869ace897309ca0ee49916536da",
    (False, True, False, True, False): "569145f32ffc28df18e12f7b1cab09578f30d2cbf7a80f4c2c0cbf0654037ade",
    (False, True, False, True, True): "ebe96c67143f10f5152ecb3e2334dc6212347efeca1928ac095971eff199fdb7",
    (False, True, True, False, False): "17670bc33d7bc6fc9df0b9dd5c38e55a143a170d8c1215369b38625a7279888a",
    (False, True, True, False, True): "9490e6a8caea0171f8e306f9401ae5b21a1c688ec59891d635fd8caa038e33a8",
    (False, True, True, True, False): "ead7f4d204b7659cc4d1e1fa32ba63ca69af65c346dd782e5203159e9d70c208",
    (False, True, True, True, True): "b98dd3476fabd1abeee4c135ad7438fb3053c521fa837f805572a7ac9597a982",
    (True, False, False, False, False): "04a6f5ad8ef13b4288eeec83a846e773206f038da6557c4d0557952101ea8ce0",
    (True, False, False, False, True): "09d084b99f239140df455012ca60f1ed32d8984cd8029473c3dc8c7c68c77d4e",
    (True, False, False, True, False): "c68cee7368970e1a0c5285db290c5c1d27ddd7778ed61a9d91d5c90b67e3f8e6",
    (True, False, False, True, True): "03bc28243fe6d52851df037db707d029afdf8bf0d634fd932c90ccf0ae055990",
    (True, False, True, False, False): "1f6714f47077f65727858ea9dea82e9627af491c1b10723e441a5d7a3987f175",
    (True, False, True, False, True): "99d22c21285b8091e233e10887413c4ec810fcef7cac541076bfa5b86bec2c40",
    (True, False, True, True, False): "d8a795675e11589b475433b43a789d5758b3d790d12d4adacb6c78afdbbbaa72",
    (True, False, True, True, True): "8278e4f885fdb4bd87886fa94b5f750ab36c331493244cb2f73bdbbfc66c38fa",
    (True, True, False, False, False): "cba94ff36a6e34bbc461eedfc615dd6d43112a85c0caa751cd0961121dafd614",
    (True, True, False, False, True): "428c8f8591739f25cbe0b6d615caa44f2a26ae87f34540d31f11b6544decbb1e",
    (True, True, False, True, False): "ded5633bc7d77e174d055628732523cfe6d45c00c13985abb5c11ced665474dc",
    (True, True, False, True, True): "3397819e0efcb09bdddcb6e38c50c8564eb7bc34295c66b95f2840e0f4bf1d3e",
    (True, True, True, False, False): "9a23f079702bdae47433f445c9496f942bf406c17b0ec15823ac24756cc4dc6c",
    (True, True, True, False, True): "88e116e8926c01c0fbb482109f39649b8d52f2c764f5dbf8bab696b1987f6eee",
    (True, True, True, True, False): "4568c2650341993a2a7ffecbc98519c211416b8f849c51824ed182f770035c70",
    (True, True, True, True, True): "9628c55145b0eef770c8bc51d08011c14f791c48990e09004b4ea4aa4e68319d",
}
FLAGS = list(itertools.product((False, True), repeat=5))


def barcelona(monkeypatch, flag: bool, spec=DONT_KNOW, deal: str = "rent", kind: str | None = "apartment") -> Request:
    monkeypatch.setenv("RENT_DEFAULT_LONG_TERM", "true" if flag else "false")
    constraints = {"deal": deal, "max_price": 1500 if deal == "rent" else 300_000, "property_type": kind}
    return request_for(constraints, location="Barcelona", vertical="real_estate", text=SOURCE_TEXT, country="ES",
                       spec=spec)


def _prompt(flags: tuple[bool, ...], lt: bool) -> tuple[str, str]:
    soft, th, hits, nr, tn = flags
    return rv.prompt_of(soft_wishes=soft, townhouse_is_house=th, quote_hits=hits, non_residential=nr, tenant=tn,
                        seasonal=lt)


# --- the words ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Piso reformado en alquiler de temporada en la Rambla del Raval",
    "alquiler temporal (máximo 11 meses) en el Barri Gòtic",
    "Apartamento en alquiler de temporada (uso exclusivo vacaciones/ocio, 32 días a 11 meses)",
    "para estancias vacacionales de más de 32 días", "solo alquiler de temporada de hasta 4 meses",
    "Se alquila por meses", "ideal para estancias cortas", "Vacation rental in Eixample",
    "Временная аренда только для отдыха (vacacional)",
    "Piso no amueblado en alquiler de temporada",
])
def test_seasonal_signs(text):
    assert seasonal_let_named(text) is True


@pytest.mark.parametrize("text", [
    "Alquiler de larga temporada", "Piso para larga estancia", "Contrato de larga duración", "alquiler no temporal",
    "No se alquila por temporada", "долгосрочная аренда, не временная аренда", "Piso de 3 habitaciones con terraza",
    None,
])
def test_no_seasonal_sign(text):
    assert seasonal_let_named(text) is False


# --- the bucket --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "row"), [(ApifyFotocasaSource, fotocasa_row),
                                             (ApifyIdealistaPortalSource, idealista_row)])
def test_portal_temporary_is_similar_when_the_term_is_unknown(monkeypatch, source, row):
    """Fotocasa ``rentalType=TEMPORARY`` / the Idealista label «Alquiler de temporada» (PORTAL_RENTAL_TERM writes it)."""
    listing = source("secret-token", rental_term=True)._normalize(row(), RENT, None)
    term = source_line_facts(text_of(listing)).get("rental_term")
    assert term == "temporary"
    request = barcelona(monkeypatch, True)
    assert request.seasonal_guard is True and request.rental_term is None
    match = classify({**FLAT, "rental_term": term}, request)
    assert (match.bucket, match.why, match.note) == ("similar", "deal_kind", SEASONAL_NOTE)


def test_summary_words_are_similar_without_a_portal_field(monkeypatch):
    request = barcelona(monkeypatch, True, spec=None)  # no term asked at all
    found = {**FLAT, "summary": "Piso de 2 habitaciones en alquiler de temporada de uso vacacional, 1400 € al mes."}
    assert (classify(found, request).bucket, classify(found, request).note) == ("similar", SEASONAL_NOTE)
    russian = {**FLAT, "summary_ru": "Квартира, временная аренда только для отдыха, 1400 € в месяц."}
    assert classify(russian, request).bucket == "similar"


def test_long_term_words_stay_exact(monkeypatch):
    request = barcelona(monkeypatch, True)
    for summary in ("Piso en alquiler de larga temporada, 1200 € al mes.", "Piso para larga estancia, no temporal."):
        assert classify({**FLAT, "summary": summary}, request).bucket == "exact"
    assert classify({**FLAT, "rental_term": "long"}, request).bucket == "exact"
    assert classify(FLAT, request).bucket == "exact"


@pytest.mark.parametrize("answer", ["на сезон", "на пару месяцев", "посуточно", "на лето"])
def test_a_short_term_asked_for_stays_exact(monkeypatch, answer):
    spec = {"context": {"answers": [{"question": TERM_QUESTION, "answer": answer}]}}
    request = barcelona(monkeypatch, True, spec=spec)
    assert request.seasonal_guard is False
    found = {**FLAT, "rental_term": "temporary", "summary": "alquiler de temporada"}
    assert classify(found, request).bucket == "exact"


def test_flag_off_as_before(monkeypatch):
    request = barcelona(monkeypatch, False)
    assert request.seasonal_guard is False
    found = {**FLAT, "rental_term": "temporary", "summary": "alquiler de temporada, uso vacacional"}
    assert classify(found, request).bucket == "exact"


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("RENT_DEFAULT_LONG_TERM", raising=False)
    assert rent_default_long_enabled() is False and Request().seasonal_guard is False
    assert settings().rent_default_long_term is False


def test_purchase_and_non_homes_are_untouched(monkeypatch):
    sale = barcelona(monkeypatch, True, deal="sale")
    assert sale.seasonal_guard is False
    found = {**FLAT, "deal_type": "sale", "price_amount": 250_000, "summary": "Piso de temporada, uso vacacional"}
    assert classify(found, sale).bucket == "exact"
    for kind in ("commercial", "land"):
        assert barcelona(monkeypatch, True, kind=kind).seasonal_guard is False
    assert barcelona(monkeypatch, True, kind=None).seasonal_guard is True  # a rent without a type is still a home


def test_seasonal_keeps_a_worse_bucket(monkeypatch):
    request = barcelona(monkeypatch, True)
    dear = {**FLAT, "price_amount": 2500, "rental_term": "temporary"}
    assert (classify(dear, request).bucket, classify(dear, request).why) == ("other", "price")


# --- the reviewer ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", FLAGS)
def test_flag_off_prompts_are_byte_for_byte_the_old_ones(flags):
    version, system = _prompt(flags, lt=False)
    assert hashlib.sha256(system.encode()).hexdigest() == OLD_SHA256[flags]
    assert "-lt" not in version and "Seasonal let" not in system


@pytest.mark.parametrize("flags", FLAGS)
def test_flag_on_prompt_has_the_rule_and_the_suffix(flags):
    old_version, _ = _prompt(flags, lt=False)
    version, system = _prompt(flags, lt=True)
    assert "-lt" in version and system.count("- Seasonal let:") == 1
    assert version == (old_version.replace("-hits", "") + "-lt" + ("-hits" if flags[2] else ""))
    assert system.index("- Seasonal let:") < system.index(rv._BEFORE_NUMBERS)
    assert "larga temporada" in system and "не знаю" in system
    assert version in final_report._LEGAL_RULE_PROMPTS


def test_all_sixty_four_versions_are_distinct_and_trusted_by_the_report():
    versions = {_prompt(flags, lt)[0] for flags in FLAGS for lt in (False, True)}
    assert len(versions) == 64 and versions == set(final_report._LEGAL_RULE_PROMPTS)


def test_parse_review_names_the_version():
    answer = ('{"criteria": [{"name": "deal", "verdict": "unknown", "quote": "alquiler de temporada"}],'
              ' "overall": "near", "deviation_ru": "сезонная аренда", "confidence": 0.8}')
    review = rv.parse_review(answer, expected=["deal"], seasonal=True)
    assert review.prompt == "reviewer-v3-lt" and review.overall == "near"
    assert rv.parse_review(answer, expected=["deal"]).prompt == "reviewer-v3"


def test_flag_reaches_the_reviewer():
    off = settings().relevance_judge()
    assert isinstance(off, ReviewerJudge) and off.reviewer.seasonal is False
    on = settings(RENT_DEFAULT_LONG_TERM="true").relevance_judge()
    assert on.reviewer.seasonal is True
