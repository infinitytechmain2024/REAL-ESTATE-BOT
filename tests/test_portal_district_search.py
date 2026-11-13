"""Q26 PORTAL_DISTRICT_SEARCH: a district of the city named in ``spec.place.districts`` reaches the portals that have a
confirmed way to take it (mocks only, no live calls).

Run N1 (rent in Madrid, Centro): the portals searched the whole city; 11 of 63 rows were in Centro. Only Idealista
``igolaizola`` changes: its coordinates rank above ``location`` (Q17), so the district goes as its centre + 2 km.
Fotocasa (``locationType`` municipality/province only) and Habitaclia (``location`` text, district not confirmed)
keep their input.
"""

import copy
import json
import pickle
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.utils import costs
from bot.web_search.geocoding import CachedCityCenters
from bot.web_search.queries import QueryTask
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    _district_ok,
    _DistrictCircle,
)

MADRID = (40.4168, -3.7038)
MADRID_BOX = (40.312, 40.643, -3.888, -3.518)
CENTRO = (40.4155, -3.7074)


def task(**kwargs):
    base = QueryTask("piso", "piso en alquiler en Madrid, Centro", "Madrid", {}, "real_estate",
                     constraints={"deal": "rent", "rooms": 2}, country_code="ES", nearby_places=("Centro",))
    return replace(base, **kwargs)


def context(name="apify_idealista", run_id=None):
    return SourceRunContext(campaign_id="campaign", source_name=name, run_id=run_id,
                            dataset_id="data1" if run_id else None, max_charge_usd=.05, launch_allowed=True,
                            save_run=AsyncMock(), reconcile_usage=AsyncMock())


def geocoder(district=CENTRO, box=None):
    found = AsyncMock()
    found.resolve.return_value = MADRID
    found.resolve_area.return_value = (MADRID, MADRID_BOX)
    found.resolve_district.return_value = (district, box) if district is not None else None
    return found


def row(code, **kwargs):
    code = f"10000000{code}"
    base = {"propertyCode": code, "url": f"https://www.idealista.com/inmueble/{code}/", "operation": "rent",
            "propertyType": "flat", "country": "es", "municipality": "Madrid", "province": "Madrid",
            "district": "Centro", "neighborhood": "Sol", "address": "Piso en Calle Mayor, Sol, Madrid",
            "price": 1500.0, "size": 70.0, "rooms": 2, "latitude": 40.416, "longitude": -3.707,
            "contactInfo": {"commercialName": "Agencia Uno", "phone1": {"phoneNumber": "900000001"}}}
    return {**base, **kwargs}


def http(rows, requests):
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        return httpx.Response(200, json=rows)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def ledger(monkeypatch):
    found = costs.MemoryLedger()
    monkeypatch.setitem(costs._state, "ledger", found)
    monkeypatch.setitem(costs._state, "budget", 0.0)
    return found


async def _run(cls, the_task, rows=(), *, name="apify_idealista", run_id=None, **kwargs):
    requests = []
    adapter = cls("secret-token", client=http(list(rows), requests), sleep=AsyncMock(), **kwargs)
    with source_run_scope(context(name, run_id)):
        found = await adapter.search(the_task, limit=5)
    await adapter.aclose()
    posts = [json.loads(r.content) for r in requests if r.method == "POST"]
    return (posts[0] if posts else None), found


def ids(found):
    return [listing.url.rsplit("/", 2)[-2][-1] for listing in found]


BEFORE = {"maxItems": 5, "operation": "rent", "propertyType": "homes", "country": "es", "sortBy": "mostRecent",
          "location": "Madrid"}


async def test_flag_off_input_is_unchanged_and_the_geocoder_is_not_asked():
    found = geocoder()
    sent, _ = await _run(ApifyIdealistaPortalSource, task(), geocoder=found)
    assert sent == BEFORE
    found.resolve_district.assert_not_awaited()


async def test_flag_off_with_the_city_circle_keeps_the_city_circle():
    sent, _ = await _run(ApifyIdealistaPortalSource, task(), geocoder=geocoder(), unambiguous_place=True)
    assert (sent["latitude"], sent["longitude"]) == MADRID and sent["distanceKm"] > 2


@pytest.mark.parametrize("unambiguous", [False, True])
async def test_flag_on_sends_the_city_name_with_the_district_centre_and_2_km(unambiguous):
    found = geocoder()
    sent, _ = await _run(ApifyIdealistaPortalSource, task(), geocoder=found, district_search=True,
                         unambiguous_place=unambiguous)
    assert sent == {**BEFORE, "latitude": 40.4155, "longitude": -3.7074, "distanceKm": 2}
    found.resolve_district.assert_awaited_once_with("Centro", "Madrid")
    found.resolve_area.assert_not_awaited()


@pytest.mark.parametrize("the_task", [
    task(nearby_places=()),  # no district
    task(nearby_places=("Centro", "Chamberí")),  # several places: one circle would lose the others
    task(place_level="province", location="Madrid"),
    task(radius_km=10.0),
])
async def test_flag_on_tasks_without_one_district_keep_their_input(the_task):
    plain, _ = await _run(ApifyIdealistaPortalSource, the_task, geocoder=geocoder())
    found = geocoder()
    sent, _ = await _run(ApifyIdealistaPortalSource, the_task, geocoder=found, district_search=True)
    assert sent == plain
    found.resolve_district.assert_not_awaited()


@pytest.mark.parametrize("failure", ["not_a_district", "error", "no_geocoder"])
async def test_flag_on_a_place_not_found_as_a_district_keeps_the_input(failure):
    found = geocoder(None)
    if failure == "error":
        found.resolve_district.side_effect = RuntimeError("redis down")
    kwargs = {} if failure == "no_geocoder" else {"geocoder": found}
    sent, _ = await _run(ApifyIdealistaPortalSource, task(nearby_places=("Getafe",)), district_search=True, **kwargs)
    assert sent == BEFORE


async def test_flag_on_rows_are_kept_for_the_district_when_they_name_one():
    rows = [row("1"), row("2", district="Chamberí", neighborhood="Trafalgar", latitude=40.43, longitude=-3.70),
            row("3", district=None, neighborhood=None), row("4", municipality="Getafe", district="Centro")]
    _, found = await _run(ApifyIdealistaPortalSource, task(), rows, geocoder=geocoder(), district_search=True)
    assert ids(found) == ["1", "3"]
    _, before = await _run(ApifyIdealistaPortalSource, task(), rows, geocoder=geocoder())
    assert ids(before) == ["1", "2", "3"]


async def test_flag_on_resumed_run_launches_nothing_and_still_filters_the_district():
    rows = [row("1"), row("2", district="Salamanca", neighborhood="Goya")]
    sent, found = await _run(ApifyIdealistaPortalSource, task(), rows, run_id="run1", geocoder=geocoder(),
                             district_search=True)
    assert sent is None and ids(found) == ["1"]


@pytest.mark.parametrize(("district", "fields", "ok"), [
    ("Centro", ("Centro", "Sol"), True), ("Sol", ("Centro", "Sol"), True),
    ("Malasaña", ("Centro", "Malasaña-Universidad"), True), ("centro", ("CENTRO", ""), True),
    ("Centro", ("Chamberí", "Trafalgar"), False), ("Sol", ("Centro", "Solana"), False),
    ("Sol", ("Sol - Centro", ""), True), ("Ciudad Lineal", ("Ciudad Universitaria", ""), False),
    ("Centro", (None, ""), True)])
def test_district_match_is_folded_and_a_row_without_one_is_kept(district, fields, ok):
    assert _district_ok(district, *fields) is ok


@pytest.mark.parametrize(("official", "portal"), [
    ("Fuencarral-El Pardo", "Fuencarral"), ("Fuencarral", "Fuencarral-El Pardo"),
    ("L'Eixample", "Eixample"), ("Eixample", "L'Eixample"), ("Ciutat Vella", "Ciutat Vella - El Gòtic"),
    ("Chamartín", "Chamartin")])
def test_official_name_matches_the_idealista_name(official, portal):
    assert _district_ok(official, portal, "")


@pytest.mark.parametrize(("official", "portal"), [
    ("Moncloa-Aravaca", "Moncloa - Aravaca"), ("Moncloa - Aravaca", "Moncloa-Aravaca"),
    ("San Blas-Canillejas", "San Blas - Canillejas"), ("Moncloa–Aravaca", "Moncloa  -  Aravaca")])
def test_hyphens_and_spaced_dashes_are_one_separator(official, portal):
    assert _district_ok(official, portal, None)


async def test_every_row_dropped_by_the_district_keeps_them_all_and_logs(caplog):
    rows = [row("1", district="Chamberí", neighborhood="Trafalgar"), row("2", district="Salamanca", neighborhood="Goya"),
            row("3", municipality="Getafe")]  # another city: dropped by the city filter, never brought back
    with caplog.at_level("WARNING"):
        _, found = await _run(ApifyIdealistaPortalSource, task(), rows, geocoder=geocoder(), district_search=True)
    assert ids(found) == ["1", "2"]
    assert any(r.message == "web_search.district_filter_empty" for r in caplog.records)


async def test_an_empty_output_or_a_kept_row_logs_nothing(caplog):
    with caplog.at_level("WARNING"):
        await _run(ApifyIdealistaPortalSource, task(), [], geocoder=geocoder(), district_search=True)
        _, found = await _run(ApifyIdealistaPortalSource, task(), [row("1"), row("2", district="Retiro",
                              neighborhood="Ibiza")], geocoder=geocoder(), district_search=True)
    assert ids(found) == ["1"]
    assert not any(r.message == "web_search.district_filter_empty" for r in caplog.records)


@pytest.mark.parametrize(("box", "km"), [
    (None, 2), ((40.405, 40.426, -3.718, -3.694), 2),  # ~3.1 km diagonal -> 1.55 -> 2
    ((40.38, 40.44, -3.75, -3.66), 5), ((40.30, 40.60, -3.90, -3.50), 6), ((40.4155, 40.4156, -3.7074, -3.7073), 1)])
async def test_district_circle_is_half_its_box_diagonal_within_1_and_6_km(box, km):
    sent, _ = await _run(ApifyIdealistaPortalSource, task(), geocoder=geocoder(box=box), district_search=True)
    assert sent["distanceKm"] == km


def test_district_circle_survives_copy_and_pickle():
    circle = _DistrictCircle(CENTRO, 2, "Centro")
    for clone in (copy.deepcopy(circle), pickle.loads(pickle.dumps(circle))):
        assert tuple(clone) == CENTRO and clone.radius_km == 2 and clone.district == "Centro"


@pytest.mark.parametrize("flag", [False, True])
async def test_fotocasa_and_habitaclia_have_no_confirmed_district_input(flag):
    """``district_search`` reaches neither: the input is the city whatever the districts."""
    fotocasa, _ = await _run(ApifyFotocasaSource, task(), name="apify_fotocasa", geocoder=geocoder())
    assert fotocasa["location"] == "Madrid" and fotocasa["locationType"] == "municipality"
    assert "latitude" not in fotocasa
    habitaclia, _ = await _run(ApifyHabitacliaSource, task(), name="apify_habitaclia")
    assert habitaclia["location"] == "Madrid"


async def test_setting_reaches_the_idealista_source_only(monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    for key, value in {"APIFY_TOKEN": "secret-token", "APIFY_IDEALISTA_ENABLED": "true",
                       "APIFY_IDEALISTA_PROVIDER": "igolaizola", "APIFY_FOTOCASA_ENABLED": "true",
                       "PORTAL_DISTRICT_SEARCH": "true"}.items():
        monkeypatch.setenv(key, value)
    sources = WebSearchSettings().sources()
    idealista = next(s for s in sources if isinstance(s, ApifyIdealistaPortalSource))
    assert idealista.district_search is True
    assert not any(hasattr(s, "district_search") for s in sources if not isinstance(s, ApifyIdealistaPortalSource))
    monkeypatch.delenv("PORTAL_DISTRICT_SEARCH")
    assert next(s for s in WebSearchSettings().sources()
                if isinstance(s, ApifyIdealistaPortalSource)).district_search is False


def nominatim(rows, requests):
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=rows)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_geocoder_finds_a_district_of_the_city_and_caches_it():
    cache = AsyncMock()
    cache.get.side_effect = [None, json.dumps(list(CENTRO))]
    requests = []
    rows = [{"name": "Centro", "addresstype": "city_district", "lat": "40.4155", "lon": "-3.7074",
             "boundingbox": ["40.405", "40.426", "-3.718", "-3.694"],
             "address": {"country_code": "es", "city_district": "Centro", "city": "Madrid"}}]
    resolver = CachedCityCenters("", cache=cache, client=nominatim(rows, requests), sleep=AsyncMock())
    box = (40.405, 40.426, -3.718, -3.694)
    assert await resolver.resolve_district("Centro", "Madrid") == (CENTRO, box)
    assert await resolver.resolve_district("Centro", "Madrid") == (CENTRO, None)  # an entry without a box
    assert len(requests) == 1 and requests[0].url.params["q"] == "Centro, Madrid"
    assert requests[0].url.params["countrycodes"] == "es"
    assert cache.setex.await_args.args[0] == "idealista:district-center:es:madrid:centro"
    assert json.loads(cache.setex.await_args.args[2]) == [*CENTRO, list(box)]
    cache.get.side_effect = [json.dumps(json.loads(cache.setex.await_args.args[2]))]
    assert await resolver.resolve_district("Centro", "Madrid") == (CENTRO, box)
    await resolver.aclose()


@pytest.mark.parametrize("found", [
    {"name": "Getafe", "addresstype": "city", "address": {"country_code": "es", "city": "Getafe"}},  # a town
    {"name": "Centro", "addresstype": "suburb", "address": {"country_code": "es", "city": "Getafe"}},  # another city
    {"name": "Calle del Centro", "addresstype": "road", "address": {"country_code": "es", "city": "Madrid"}},
    {"name": "Centro", "addresstype": "shop", "address": {"country_code": "es", "city": "Madrid"}},
    {"name": "Centro", "addresstype": "suburb", "address": {"country_code": "pt", "city": "Madrid"}},
])
async def test_geocoder_refuses_what_is_not_a_district_of_the_city(found):
    cache = AsyncMock()
    cache.get.return_value = None
    rows = [{**found, "lat": "40.3", "lon": "-3.7"}]
    resolver = CachedCityCenters("", cache=cache, client=nominatim(rows, []), sleep=AsyncMock())
    name = "Getafe" if found["name"] == "Getafe" else "Centro"
    assert await resolver.resolve_district(name, "Madrid") is None
    cache.setex.assert_not_awaited()
    await resolver.aclose()


async def test_geocoder_without_a_cache_never_asks_nominatim():
    requests = []
    resolver = CachedCityCenters("", client=nominatim([], requests), sleep=AsyncMock())
    assert await resolver.resolve_district("Centro", "Madrid") is None and requests == []
    await resolver.aclose()
