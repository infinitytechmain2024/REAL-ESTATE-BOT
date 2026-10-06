"""Task kind, budget/rooms, city vs region, country and portals by kind in the web-search queries."""

from __future__ import annotations

import pytest

from bot.campaign import geo
from bot.web_search.models import GeneratedQuery
from bot.web_search.queries import (
    QueryTask,
    TemplateQueryGenerator,
    localise,
    place_level_of,
    portal_query,
    task_kind,
)
from bot.web_search.urls import SPAIN_PORTALS, SPAIN_PORTALS_BY_KIND


def task(text: str, *, location: str = "Valencia", **constraints) -> QueryTask:
    aliases = {"es": location, "en": location, "ru": "Валенсия", "uk": "Валенсія"} if location == "Valencia" \
        else {"es": location, "en": location}
    return QueryTask(goal=text, task_text=text, location=location, location_aliases=aliases,
                     vertical="real_estate", constraints=dict(constraints))


@pytest.mark.parametrize(("text", "kind"), [
    ("apartment in Valencia under 200k, 2+ rooms", "apartment"),
    ("квартира 2 комнаты Валенсия", "apartment"),
    ("комната в Мадриде", "room"),
    ("habitación en Madrid", "room"),
    ("piso 3 habitaciones", "apartment"),
    ("dos habitaciones en Valencia", "apartment"),
    ("от 2 комнат Валенсия", "apartment"),
    ("2 rooms flat", "apartment"),
    ("room for rent in Madrid", "room"),
    ("terreno en Valencia", "land"),
    ("casa con terreno", "land"),
    ("local comercial en Valencia", "commercial"),
    ("flat near the location of the sea", "apartment"),
    ("piso en la localidad de Torrent", "apartment"),
    ("house in Poland-style area", "house"),
])
def test_task_kind(text: str, kind: str) -> None:
    assert task_kind(task(text)) == kind


def test_budget_and_rooms_in_every_language() -> None:
    t = task("flat", deal="sale", max_price=200000, rooms=2)
    assert "hasta 200000" in portal_query(t, "idealista.com").text
    assert "2 habitaciones" in portal_query(t, "idealista.com").text
    texts = {q.language: q.text for q in TemplateQueryGenerator().candidates(t)}
    assert "under 200000" in texts["en"] and "2 bedrooms" in texts["en"]
    assert "до 200000" in texts["ru"] and "2 комнаты" in texts["ru"]
    assert "до 200000" in texts["uk"] and "2 кімнати" in texts["uk"]


def test_no_budget_when_unknown_and_none_for_investors() -> None:
    assert "hasta" not in portal_query(task("piso", deal="sale"), "idealista.com").text
    inv = QueryTask(goal="инвесторы", task_text="инвесторы", location="Valencia", location_aliases={"es": "Valencia"},
                    vertical="investors", constraints={"max_price": 100000, "rooms": 2})
    assert all("hasta" not in q.text and "habitaciones" not in q.text for q in TemplateQueryGenerator().candidates(inv))


def test_golden_apartment_valencia() -> None:
    t = task("apartment in Valencia under 200k, 2+ rooms, sale", deal="sale", max_price=200000, rooms=2)
    assert task_kind(t) == "apartment"
    q = portal_query(t, "idealista.com").text
    for part in ("piso", "en venta", "hasta 200000", "2 habitaciones", "Valencia", "España"):
        assert part in q
    assert t.portals()[:4] == ("idealista.com", "fotocasa.es", "habitaclia.com", "pisos.com")


def test_place_level() -> None:
    assert place_level_of("piso en Valencia", "Valencia") == "city"
    assert place_level_of("piso en la Comunidad Valenciana", "Valencia") == "region"
    assert place_level_of("дом в провинции Аликанте", "Alicante") == "region"
    assert task("piso").place_level == "city"


def test_a_region_is_not_the_city_but_a_region_task_accepts_it() -> None:
    q = [GeneratedQuery("piso Comunitat Valenciana", "es"), GeneratedQuery("pisos valenciana venta", "es")]
    out = [x.text for x in localise(q, task("piso en Valencia"))]
    assert out == ["piso Comunitat Valenciana Valencia España", "pisos valenciana venta Valencia España"]
    regional = QueryTask("piso", "piso", "Valencia", {"es": "Valencia"}, "real_estate", place_level="region")
    assert [x.text for x in localise(q[:1], regional)] == ["piso Comunitat Valenciana España"]


def test_mentions_place_strict_is_whole_word() -> None:
    names = geo.place_names("Valencia", {"es": "Valencia", "ru": "Валенсия"}, regions=False)
    assert geo.mentions_place("pisos valenciana", names)  # the old substring rule
    assert not geo.mentions_place("pisos valenciana", names, strict=True)
    assert not geo.mentions_place("валенсийское сообщество", names, strict=True)
    assert geo.mentions_place("квартира в Валенсии", names, strict=True)


def test_country_is_appended_once_in_the_query_language() -> None:
    t = task("piso")
    out = localise([GeneratedQuery("pisos Valencia", "es"), GeneratedQuery("flats Valencia Spain", "en"),
                    GeneratedQuery("flat in Valencia", "en"), GeneratedQuery("квартира Valencia", "ru"),
                    GeneratedQuery("квартира Валенсия", "uk")], t)
    assert [q.text for q in out] == ["pisos Valencia España", "flats Valencia Spain", "flat in Valencia Spain",
                                     "квартира Valencia Испания", "квартира Валенсия Valencia Іспанія"]
    kyiv = QueryTask("квартира", "квартира", "Kyiv", {"uk": "Київ"}, "real_estate")
    assert localise([GeneratedQuery("квартира Київ", "uk")], kyiv)[0].text == "квартира Київ Україна"
    unknown = QueryTask("flat", "flat", "Ubud", {"en": "Ubud"}, "real_estate")
    assert localise([GeneratedQuery("flat Ubud", "en")], unknown)[0].text == "flat Ubud"


def test_template_generator_adds_one_suburb_query_and_the_country() -> None:
    out = TemplateQueryGenerator().candidates(task("piso", deal="sale"))
    assert sum(("afueras" in q.text or " near " in q.text or "пригород" in q.text or "передмістя" in q.text)
               for q in out) == 1


def test_foreign_markers() -> None:
    assert geo.foreign_markers_hit("ES", "Apartamento en Valencia, Carabobo")
    assert geo.foreign_markers_hit("ES", "Houses in Valencia, CA for 300,000 USD")
    assert geo.foreign_markers_hit("ES", "Piso Valencia Venezuela")
    assert not geo.foreign_markers_hit("ES", "Piso en Valencia, Comunitat Valenciana 180.000 €")
    assert not geo.foreign_markers_hit("ES", "Casa en Alicante, 3 dormitorios, usdos")
    assert not geo.foreign_markers_hit("UA", "Venezuela")
    assert not geo.foreign_markers_hit(None, "Venezuela")


def test_portals_by_kind_and_bank_portals_on_request() -> None:
    assert task("terreno en Valencia").portals() == SPAIN_PORTALS_BY_KIND["land"]
    assert task("local comercial en Valencia").portals() == SPAIN_PORTALS_BY_KIND["commercial"]
    assert task("habitación en Valencia").portals() == SPAIN_PORTALS_BY_KIND["room"]
    assert task("casa en Valencia").portals() == SPAIN_PORTALS_BY_KIND["house"]
    assert "solvia.es" not in task("piso en Valencia").portals()
    bank = task("piso barato de banco en Valencia").portals()
    assert bank[:11] == SPAIN_PORTALS_BY_KIND["apartment"] and {"solvia.es", "sareb.es", "haya.es"} <= set(bank)
    land_bank = task("terreno Sareb en Valencia").portals()
    assert land_bank.count("sareb.es") == 1 and "haya.es" in land_bank
    assert set(bank) <= set(SPAIN_PORTALS)
    assert task("flat", location="Madrid").portals()[:2] == ("idealista.com", "fotocasa.es")
