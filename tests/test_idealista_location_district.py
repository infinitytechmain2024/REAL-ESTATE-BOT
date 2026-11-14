"""Q57 IDEALISTA_LOCATION_DISTRICT: the row's ``neighborhood``/``district`` reach the listing's place (the address the
reviewer sees), so a task naming the district is confirmed (mocks only, no live calls).

Run b5-g3-s6 (a house in Marbella, «Nueva Andalucía, Golden Mile»): the row's ``address`` named only «Puerto Banús»,
its ``district`` «Nueva Andalucía» was dropped, and the reviewer wrote «Не подтверждено: место».
"""

from dataclasses import replace

import pytest

from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.portals import ApifyIdealistaPortalSource


def task():
    return QueryTask("casa", "casa en venta en Marbella", "Marbella", {}, "real_estate",
                     constraints={"deal": "sale"}, country_code="ES",
                     nearby_places=("Nueva Andalucía", "Golden Mile"))


def row(**kwargs):
    base = {"url": "https://www.idealista.com/inmueble/100000001/", "operation": "sale", "propertyType": "chalet",
            "country": "es", "municipality": "Marbella", "province": "Málaga", "district": "Nueva Andalucía",
            "neighborhood": "Puerto Banús", "address": "Chalet pareado en Puerto Banús, Marbella",
            "price": 900000, "size": 200, "rooms": 3, "description": "Chalet pareado."}
    return base | kwargs


def place(item, **flags):
    listing = ApifyIdealistaPortalSource("token", **flags)._normalize(item, task(), None)
    assert listing is not None
    return listing.address


def test_off_keeps_the_address_as_before():
    assert place(row()) == "Chalet pareado en Puerto Banús, Marbella, Málaga"
    assert place(row(), location_district=False) == place(row())


def test_on_adds_the_district_once():
    assert place(row(), location_district=True) == "Chalet pareado en Puerto Banús, Marbella, Nueva Andalucía, Málaga"


def test_on_adds_neighbourhood_and_district_not_in_the_address():
    item = row(address="Chalet en Calle Uno", neighborhood="Las Brisas")
    assert place(item, location_district=True) == "Chalet en Calle Uno, Las Brisas, Nueva Andalucía, Marbella, Málaga"


def test_on_without_hierarchy_fields_is_as_before():
    item = row(district=None, neighborhood=None)
    assert place(item, location_district=True) == place(item) == "Chalet pareado en Puerto Banús, Marbella, Málaga"


def test_on_no_duplicate_when_district_is_the_neighbourhood():
    item = row(address="Piso en La Quinta, Benahavís", municipality="Benahavís", neighborhood="La Quinta",
               district="La Quinta")
    item["url"] = "https://www.idealista.com/inmueble/100000002/"
    listing = ApifyIdealistaPortalSource("token", location_district=True)._normalize(
        item, replace(task(), location="Benahavís", nearby_places=()), None)
    assert listing is not None and listing.address == "Piso en La Quinta, Benahavís, Málaga"


@pytest.mark.parametrize(("fields", "expected"), [
    ({"neighborhood": "Centro", "district": "Marbella"}, "Chalet en Calle Uno, Centro, Marbella, Málaga"),
    ({"neighborhood": "PUERTO BANÚS", "district": "Puerto Banús"}, "Chalet en Calle Uno, PUERTO BANÚS, Marbella, Málaga"),
    ({"neighborhood": "Nueva Andalucia", "district": "Nueva Andalucía"},
     "Chalet en Calle Uno, Nueva Andalucia, Marbella, Málaga"),
    ({"neighborhood": "Nueva Andalucía - Puerto Banús", "district": "Nueva Andalucía"},
     "Chalet en Calle Uno, Nueva Andalucía - Puerto Banús, Marbella, Málaga"),
    ({"neighborhood": 123, "district": {"x": 1}}, "Chalet en Calle Uno, Marbella, Málaga"),
])
def test_on_no_duplicates_by_case_accent_or_contained_name(fields, expected):
    assert place(row(address="Chalet en Calle Uno", **fields), location_district=True) == expected


def test_on_street_names_are_not_searched():
    item = row(address="Piso en Calle Centro Comercial, Marbella", neighborhood="Centro", district="Casco Antiguo")
    assert place(item, location_district=True) == "Piso en Calle Centro Comercial, Marbella, Centro, Casco Antiguo, Málaga"


def test_on_whole_words_not_substrings():
    item = row(address="Piso en Calle Uno, Marbella", neighborhood="Elviria", district="Marbella Este")
    assert place(item, location_district=True) == "Piso en Calle Uno, Marbella, Elviria, Marbella Este, Málaga"


async def test_setting_reaches_the_source(monkeypatch):
    for name in ("IDEALISTA_LOCATION_DISTRICT", "APIFY_IDEALISTA_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    env = {"APIFY_TOKEN": "token", "APIFY_IDEALISTA_ENABLED": True, "APIFY_IDEALISTA_PROVIDER": "igolaizola"}
    off, = WebSearchSettings(_env_file=None, **env).sources()
    on, = WebSearchSettings(_env_file=None, IDEALISTA_LOCATION_DISTRICT=True, **env).sources()
    try:
        assert off.location_district is False and on.location_district is True
    finally:
        await off.aclose()
        await on.aclose()
