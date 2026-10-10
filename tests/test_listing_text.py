"""Area and deal read from real Spanish listing text (bot/utils/listing_text.py) and the fixes around it.

The «участок, Мадрид, покупка, ≥2000 м²» run rejected Pisos.com plots as «не та площадь»: a house's built area won
over its plot, «2.000 m²» read as 2 passed the evidence check because of the «2» in «m2», «2ha» was 2 m².
"""

from __future__ import annotations

import json

import pytest

from bot.analysis_pipeline.models import Evidence
from bot.campaign.tolerance import min_area_of
from bot.utils.listing_text import areas, deal_of, largest_area, number, plot_area
from bot.web_search.structured import _area, from_jsonld


@pytest.mark.parametrize(("raw", "value"), [
    ("2.000", 2000), ("2.000,5", 2000.5), ("2,5", 2.5), ("1.5", 1.5), ("2 000", 2000), ("12.500", 12500),
    ("1.250.000", 1_250_000), ("180", 180),
])
def test_spanish_numbers(raw: str, value: float) -> None:
    assert number(raw) == value


@pytest.mark.parametrize(("text", "plot", "largest"), [
    ("Terreno urbano en venta en Boadilla del Monte, parcela de 2.000 m²", 2000, 2000),
    ("Chalet de 180 m² construidos con parcela de 2.500 m² en Pozuelo de Alarcón", 2500, 2500),
    ("Superficie construida: 180 m2 · Superficie útil: 150 m2", None, 180),
    ("Finca rústica de 2,5 ha en Colmenar Viejo", 25000, 25000),
    ("Terreno rústico de 1.5 hectáreas", 15000, 15000),
    ("Solar de 3.250 metros cuadrados en Getafe", 3250, 3250),
    ("Parcela: 850 m² · a 500 m del metro · a 200 metros del colegio", 850, 850),
    ("Участок 20 соток под Мадридом", 2000, 2000),
    ("Piso de 85 m² en Lavapiés, 3 habitaciones", None, 85),
    ("Se vende terreno en Arganda, 12.500 m2, 375.000 €", 12500, 12500),
])
def test_areas_from_spanish_listings(text: str, plot: float | None, largest: float) -> None:
    assert plot_area(text) == plot
    assert largest_area(text) == largest


def test_distances_and_prices_are_not_areas() -> None:
    assert areas("A 500 m del metro y a 2 km de la playa, 450.000 €") == []
    assert [a.kind for a in areas("Vivienda de 120 m² en parcela de 1.000 m²")] == ["built", "plot"]


@pytest.mark.parametrize(("text", "deal"), [
    ("Terreno en venta en Madrid", "sale"), ("Se vende parcela urbanizable", "sale"),
    ("Se alquila parcela para huerto, 300 €/mes", "rent"), ("Terreno en alquiler en Getafe", "rent"),
    ("Nave en arrendamiento", "rent"), ("Venta · Alquiler · Obra nueva", None), ("Parcela urbanizable 2.000 m²", None),
    ("Land plot for sale near Madrid", "sale"), ("Plot to let, 500 €/month", "rent"),
])
def test_deal_from_strong_signals_only(text: str, deal: str | None) -> None:
    assert deal_of(text) == deal


def test_json_ld_keeps_the_plot_apart_from_the_built_area() -> None:
    house = {"@type": "SingleFamilyResidence", "name": "Chalet con parcela", "url": "https://www.pisos.com/comprar/x-123456789/",
             "floorSize": {"value": "180", "unitCode": "MTK"}, "lotSize": {"value": "2.500", "unitCode": "MTK"},
             "offers": {"price": "450000", "priceCurrency": "EUR"}}
    listing = from_jsonld([json.dumps(house)], house["url"]).listings[0]
    assert (listing["area_m2"], listing["plot_m2"]) == (180, 2500)  # both facts, the plot no longer lost
    plot = {"@type": ["Product", "LandParcel"], "name": "Parcela", "floorSize": {"value": "999"},
            "lotSize": {"value": "2000"}, "offers": {"price": 300000}}
    assert from_jsonld([json.dumps(plot)], "https://x.es/p").listings[0]["area_m2"] == 2000  # a plot of land: its lot
    only_lot = {"@type": "Product", "name": "Terreno", "lotSize": "3.000 m²", "offers": {"price": 1}}
    assert from_jsonld([json.dumps(only_lot)], "https://x.es/t").listings[0]["area_m2"] == 3000


@pytest.mark.parametrize(("raw", "m2"), [("2ha", 20000), ("2 ha", 20000), ("2.000 m²", 2000), ("1,5 hectáreas", 15000),
                                          ({"value": "3", "unitCode": "HAR"}, 30000)])
def test_json_ld_area_units(raw: object, m2: float) -> None:
    assert _area(raw) == m2


def test_an_area_misread_as_two_is_not_backed_by_the_m2_unit() -> None:
    from bot.analysis_pipeline.openrouter import parse_result, verify_facts
    from tests.test_analysis_pipeline import V6

    text = "Terreno urbano en venta, parcela de 2.000 m2 en Madrid. Precio 300.000 €."
    misread = parse_result(json.dumps({**V6, "area_m2": 2.0, "evidence": {**V6["evidence"], "area": "parcela de 2.000 m2"}}))
    assert verify_facts(misread, Evidence(post_id="p", source_id="s", canonical_url="https://x.es/1", text=text, title="t")).area_m2 is None
    right = parse_result(json.dumps({**V6, "area_m2": 2000, "evidence": {**V6["evidence"], "area": "parcela de 2.000 m2"}}))
    assert verify_facts(right, Evidence(post_id="p", source_id="s", canonical_url="https://x.es/1", text=text, title="t")).area_m2 == 2000


@pytest.mark.parametrize(("task", "m2"), [
    ("участок от 2000 м² в Мадриде, покупка", 2000), ("участок 2000 м² и больше", 2000), ("parcela 2.000 m2 o más", 2000),
    ("≥2000 м2", 2000), ("plot 2000 sqm or more", 2000), ("минимум 0,2 га", 2000), ("участок 2000 м²", None),
])
def test_min_area_of_the_task(task: str, m2: float | None) -> None:
    assert min_area_of(task) == m2
