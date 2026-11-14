"""Q38 PORTAL_PROVINCE_BY_CODE: a province task keeps a portal row whose province has the task's INE code
(``bot/web_search/provinces.py``), so «Alacant» in the task and «Alicante» in the row are one province (mocks only)."""

from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.utils import costs
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    ApifyMilanunciosSource,
    ApifyPisosSource,
)

PORTALS = (ApifyIdealistaPortalSource, ApifyFotocasaSource, ApifyPisosSource, ApifyHabitacliaSource,
           ApifyMilanunciosSource)


def task(location="Alacant", **kwargs):
    base = QueryTask("terreno", "terreno", location, {}, "real_estate", constraints={"deal": "sale"},
                     country_code="ES", place_level="province")
    return replace(base, **kwargs)


def ok(cls, the_task, province, *, on=True, municipality="Elche"):
    source = cls("secret-token", province_by_code=on)
    return source._place_ok(the_task, None, None, municipality, province)


@pytest.mark.parametrize("cls", PORTALS)
@pytest.mark.parametrize(("wanted", "row"), [
    ("Alacant", "Alicante"), ("Alicante", "Alacant/Alicante"), ("Vizcaya", "Bizkaia"), ("Bizkaia", "Vizcaya"),
    ("La Coruña", "A Coruña"), ("Castelló", "Castellón"), ("Araba", "Álava"), ("Gerona", "Girona"),
    ("Lérida", "Lleida"), ("Orense", "Ourense"), ("Guipúzcoa", "Gipuzkoa"), ("Baleares", "Illes Balears"),
    ("Comunidad de Madrid", "Madrid"),
])
def test_synonyms_pass_with_the_flag(cls, wanted, row):
    assert ok(cls, task(wanted), row)
    assert not ok(cls, task(wanted), row, on=False)  # the old name comparison drops them


@pytest.mark.parametrize("cls", PORTALS)
def test_other_provinces_do_not_pass(cls):
    assert not ok(cls, task("Alacant"), "Valencia")
    assert not ok(cls, task("Vizcaya"), "Gipuzkoa")
    assert not ok(cls, task("Madrid"), "Toledo")


@pytest.mark.parametrize("cls", PORTALS)
def test_unlisted_name_keeps_the_name_comparison(cls):
    for on in (True, False):
        assert ok(cls, task("Andalucía"), "Andalucía", on=on)
        assert not ok(cls, task("Andalucía"), "Málaga", on=on)
        # «Tenerife» is an island, not a province: not in the list, compared by name as before.
        assert ok(cls, task("Tenerife"), "Tenerife", on=on)
        assert not ok(cls, task("Tenerife"), "Santa Cruz de Tenerife", on=on)
        assert not ok(cls, task("Alacant"), "", on=on)
        assert not ok(cls, task("Alacant"), "Provincia Desconocida", on=on)


@pytest.mark.parametrize("cls", PORTALS)
def test_flag_off_is_the_old_rule(cls):
    assert ok(cls, task("Alicante"), "alicante", on=False)
    assert ok(cls, task("Alacant", nearby_places=("Alicante",)), "Alicante", on=False)
    assert not ok(cls, task("Alacant"), "Alicante", on=False)


@pytest.mark.parametrize("cls", PORTALS)
def test_city_and_municipality_unchanged(cls):
    city = task("Alacant", place_level="city")
    assert not ok(cls, city, "Alicante", municipality="Alicante")  # a city is still its own name
    assert ok(cls, city, "Alicante", municipality="Alacant")
    assert not ok(cls, task("Elche", place_level="city"), "Alicante", municipality="Alicante")


@pytest.mark.parametrize("on", [True, False])
def test_wrong_place_follows_the_rule(on):
    source = ApifyIdealistaPortalSource("secret-token", province_by_code=on)
    assert source._place_wrong(task("Alacant"), None, None, "Elche", "Alicante") is not on
    assert source._place_wrong(task("Alacant"), None, None, "Valencia", "Valencia")


def context():
    return SourceRunContext(campaign_id="campaign", source_name="apify_idealista", run_id=None, dataset_id=None,
                            max_charge_usd=.05, launch_allowed=True, save_run=AsyncMock(), reconcile_usage=AsyncMock())


@pytest.fixture(autouse=True)
def ledger(monkeypatch):
    found = costs.MemoryLedger()
    monkeypatch.setitem(costs._state, "ledger", found)
    monkeypatch.setitem(costs._state, "budget", 0.0)
    return found


def row(number, municipality, province):
    return {"url": f"https://www.idealista.com/inmueble/10000030{number}/", "operation": "sale", "propertyType": "land",
            "country": "es", "municipality": municipality, "province": province, "price": 100000, "size": 500}


async def _search(the_task, rows, **kwargs):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"usageTotalUsd": .01}})
        return httpx.Response(200, json=rows)

    adapter = ApifyIdealistaPortalSource("secret-token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                         sleep=AsyncMock(), **kwargs)
    with source_run_scope(context()):
        found = await adapter.search(the_task, limit=10)
    await adapter.aclose()
    return [listing.url for listing in found]


async def test_idealista_rows_of_alacant_and_madrid():
    rows = [row(1, "Elche", "Alicante"), row(2, "Valencia", "Valencia")]
    assert await _search(task("Alacant"), rows, province_by_code=True) == ["https://www.idealista.com/inmueble/100000301/"]
    assert await _search(task("Alacant"), rows) == []
    madrid = [row(3, "Navalcarnero", "Madrid"), row(4, "Illescas", "Toledo")]
    for on in (True, False):
        found = await _search(task("Madrid"), madrid, province_by_code=on)
        assert found == ["https://www.idealista.com/inmueble/100000303/"]


def test_settings_pass_the_flag(monkeypatch):
    for name in ("PORTAL_PROVINCE_BY_CODE", "APIFY_IDEALISTA_PROVIDER", "APIFY_FOTOCASA_ENABLED",
                 "APIFY_PISOS_ENABLED", "APIFY_HABITACLIA_ENABLED", "APIFY_MILANUNCIOS_ENABLED",
                 "APIFY_YAENCONTRE_ENABLED", "APIFY_IDEALISTA_ENABLED", "APIFY_TOKEN", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    env = {"APIFY_TOKEN": "secret-token", "APIFY_IDEALISTA_ENABLED": True, "APIFY_IDEALISTA_PROVIDER": "igolaizola",
           "APIFY_FOTOCASA_ENABLED": True, "APIFY_PISOS_ENABLED": True, "APIFY_HABITACLIA_ENABLED": True,
           "APIFY_MILANUNCIOS_ENABLED": True}
    off = WebSearchSettings(_env_file=None, **env).sources()
    on = WebSearchSettings(_env_file=None, PORTAL_PROVINCE_BY_CODE=True, **env).sources()
    assert {type(s) for s in on} == set(PORTALS)
    assert all(s.province_by_code is False for s in off) and all(s.province_by_code is True for s in on)
