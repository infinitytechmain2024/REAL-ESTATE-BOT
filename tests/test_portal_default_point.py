"""Q12 PORTAL_DEFAULT_POINT_GUARD: a Fotocasa row at the portal's fallback point of its district (mocks only).

The rows are synthetic: the shape of ``igolaizola/fotocasa-scraper`` output, made-up phones, agencies and texts; the
coordinates are jittered around the fallback points the way the live output was (up to ~0.3 km).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from bot.analysis_pipeline.formatters import finding_payload, source_line_facts
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer
from bot.campaign.tolerance import Request, classify, default_point_guard_enabled, request_for
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.portals import ApifyFotocasaSource, default_point
from bot.web_search.worker import WebSearchConfig, WebSearchWorker

_REAL_CLIENT = httpx.AsyncClient
TASK = QueryTask("terreno", "terreno", "Madrid", {}, "real_estate", constraints={"deal": "sale"}, country_code="ES")


def row(**kwargs):
    base = {
        "url": "https://www.fotocasa.es/es/comprar/terreno/madrid/ciudad-universitaria/100000001/d",
        "transaction": {"type": "SALE", "price": 90000}, "surface": 5000, "rooms": None, "phone": "+34900000001",
        "street": None, "agency": {"name": "Agencia Ejemplo S.L."},
        "location": {"level1Name": "Madrid", "level2Name": "Madrid", "level5Name": "Madrid",
                     "level7Name": "Moncloa - Aravaca", "level8Name": "Ciudad Universitaria",
                     "latitude": "40.4631", "longitude": "-3.7489"},
        "description": "",
    }
    return {**base, **kwargs}


NORMAL = row(url="https://www.fotocasa.es/es/comprar/terreno/madrid/la-florida-el-plantio/100000002/d",
             street="Calle Ejemplo",
             location={"level1Name": "Madrid", "level2Name": "Madrid", "level5Name": "Madrid",
                       "level7Name": "Moncloa - Aravaca", "level8Name": "La Florida -  El  Plantío",
                       "latitude": "40.4735", "longitude": "-3.8253"})


def text_of(listing) -> str:
    return WebSearchWorker._source_text(SimpleNamespace(config=WebSearchConfig()), listing)


ANSWER = {
    "relevant": True, "confidence": 0.9, "summary": "Terreno", "location": None, "price_signals": ["90000"],
    "related_links": [], "category": "real_estate", "reason": "offer", "summary_ru": "Участок",
    "source_language": "es", "price_amount": 90000, "price_currency": "EUR", "deal_type": "sale",
    "property_type": "land", "rooms": None, "who": None, "listing_kind": "offer", "country": "ES", "area_m2": 5000,
    "evidence": {"price": "90000", "area": "5000", "rooms": None, "location": None}, "district": None,
    "address": None, "floor": None, "features": [], "condition": None, "listing_date": None,
}


async def payload(monkeypatch, listing, answer: dict) -> dict:
    """The finding payload the analysis writes for the post the web worker built from ``listing``."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    evidence = Evidence(post_id="p", source_id="s", canonical_url=listing.url, title=listing.title,
                        text=text_of(listing), platform="website")
    result = await OpenRouterAnalyzer("key", "m").analyze(evidence, "real_estate")
    return finding_payload(result, evidence)


def madrid(monkeypatch, flag: bool) -> Request:
    monkeypatch.setenv("PORTAL_DEFAULT_POINT_GUARD", "true" if flag else "false")
    return request_for({"deal": "sale"}, location="Madrid", vertical="real_estate", country="ES")


@pytest.mark.parametrize(("name", "lat", "lon", "hit"), [
    ("Ciudad Universitaria", 40.4627, -3.7527, True),  # 0.32 km off the point: the portal's jitter
    ("Cortes - Huertas", 40.4181, -3.7001, True),
    ("Sol", 40.4163, -3.7011, True),
    ("sol", 40.4182, -3.7057, True),
    ("Ciudad Universitaria", 40.4700, -3.7490, False),  # 0.8 km away: a real place in the district
    ("La Florida -  El  Plantío", 40.4625, -3.749, False),  # another district at the point
    ("Sol", None, None, False),
])
def test_default_point_is_the_fallback_point_of_the_rows_own_district(name, lat, lon, hit):
    assert default_point(name, None if lat is None else (lat, lon)) is hit


def test_flag_off_by_default_everywhere(monkeypatch):
    monkeypatch.delenv("PORTAL_DEFAULT_POINT_GUARD", raising=False)
    assert WebSearchSettings(_env_file=None).portal_default_point_guard is False
    assert default_point_guard_enabled() is False and Request().place_guard is False
    assert ApifyFotocasaSource("secret-token").default_point_guard is False


@pytest.mark.asyncio
async def test_settings_pass_the_flag_to_fotocasa():
    settings = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", APIFY_FOTOCASA_ENABLED=True,
                                 PORTAL_DEFAULT_POINT_GUARD=True)
    source, = settings.sources()
    try:
        assert isinstance(source, ApifyFotocasaSource) and source.default_point_guard is True
    finally:
        await source.aclose()


@pytest.mark.asyncio
async def test_flag_off_keeps_the_portals_place_and_the_finding_stays_exact(monkeypatch):
    listing = ApifyFotocasaSource("secret-token")._normalize(row(), TASK, None)
    assert listing.address == "Ciudad Universitaria, Madrid" and listing.title == "Terreno en venta, Ciudad Universitaria"
    assert listing.place_unverified is None
    first = text_of(listing).split("\n", 1)[0]
    assert "place_unverified" not in first and source_line_facts(text_of(listing)).get("place_unverified") is None
    found = await payload(monkeypatch, listing, {**ANSWER, "location": "Ciudad Universitaria, Madrid"})
    assert "place_unverified" not in found
    assert classify(found, madrid(monkeypatch, False)).bucket == "exact"


@pytest.mark.asyncio
async def test_flag_on_a_row_at_the_default_point_without_text_goes_to_similar(monkeypatch):
    listing = ApifyFotocasaSource("secret-token", default_point_guard=True)._normalize(row(), TASK, None)
    assert listing.place_unverified is True and listing.address is None and listing.title == "Terreno en venta"
    first = text_of(listing).split("\n", 1)[0]
    assert "Madrid" not in first and "Ciudad Universitaria" not in first and '"place_unverified": true' in first
    assert (listing.price, listing.plot_m2, listing.phone) == (90000, 5000, "+34900000001")  # facts stay as given
    found = await payload(monkeypatch, listing, ANSWER)  # no description: the analysis names no place
    assert found["location"] is None and found["place_unverified"] is True
    match = classify(found, madrid(monkeypatch, True))
    assert (match.bucket, match.why) == ("similar", "location_unknown")
    # a place the model read off the link slug does not make it exact either
    slug = await payload(monkeypatch, listing, {**ANSWER, "location": "Madrid"})
    assert classify(slug, madrid(monkeypatch, True)).bucket == "similar"
    # the same payload with the flag rolled back: the marker is ignored, exact as before
    assert classify(found, madrid(monkeypatch, False)).bucket == "exact"


@pytest.mark.asyncio
async def test_flag_on_a_described_row_at_the_default_point_in_another_town_is_other(monkeypatch):
    described = row(description="Finca rústica de 5.000 m² en Valencia, junto a la huerta.")
    listing = ApifyFotocasaSource("secret-token", default_point_guard=True)._normalize(described, TASK, None)
    assert listing.place_unverified is True and listing.address is None
    found = await payload(monkeypatch, listing, {**ANSWER, "location": "Valencia"})
    assert classify(found, madrid(monkeypatch, True)).bucket == "other"


@pytest.mark.asyncio
async def test_flag_on_an_ordinary_row_stays_exact(monkeypatch):
    listing = ApifyFotocasaSource("secret-token", default_point_guard=True)._normalize(NORMAL, TASK, None)
    assert listing.place_unverified is None
    assert listing.address == "Calle Ejemplo, La Florida -  El  Plantío, Madrid"
    found = await payload(monkeypatch, listing, {**ANSWER, "location": "La Florida, Madrid"})
    assert "place_unverified" not in found
    assert classify(found, madrid(monkeypatch, True)).bucket == "exact"


def test_location_unknown_is_counted_and_offered_as_not_confirmed():
    from bot.campaign.relevance import reason_category

    assert reason_category("location_unknown") == "unverified"
