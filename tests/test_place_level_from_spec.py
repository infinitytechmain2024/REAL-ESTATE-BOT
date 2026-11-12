"""v3 Q11: SEARCH_PLACE_LEVEL_FROM_SPEC -- the place level of the TaskSpec reaches the portal actors (mocks only).

The G1 pilot: «вся провинция Мадрид» was in ``spec.place.level`` but not in the campaign text, so the portals searched
the municipality of Madrid and the 13 715 m² plot in Fuentidueña de Tajo (a row of the live Milanuncios fixture) was
dropped as ``apify_invalid_listing``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.spec import TaskSpec
from bot.web_search.queries import portal_place_level
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.apify import ApifyIdealistaSource, SourceRunContext, source_run_scope
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyIdealistaPortalSource,
    ApifyMilanunciosSource,
)
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, query_task
from tests.test_apify_portals import fotocasa_row, idealista_row, live_rows

GOAL = "покупка Тип: участок. Задача: участок от 2000 м² в Мадриде"
FUENTIDUENA = "537313151"  # the id of the Fuentidueña de Tajo row in milanuncios_items_madrid_solares.json


def _spec(level: str) -> TaskSpec:
    return TaskSpec.model_validate({"mode": "real_estate", "deal": "sale", "property_type": "land",
                                    "place": {"name": "Madrid", "country": "ES", "level": level},
                                    "area_m2": {"min": 2000}})


async def _campaign(level: str = "province", text: str = GOAL):
    campaigns = MemoryCampaignStore()
    spec = _spec(level)
    plan = plan_campaign(text, vertical="real_estate", spec=spec)
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text=text, actor="test",
                                 spec=spec.model_dump(mode="json"))
    return await campaigns.get(cid)


def _api(rows, requests):
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"usageTotalUsd": .01}})
        return httpx.Response(200, json=rows)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _context(name):
    return SourceRunContext(campaign_id="campaign", source_name=name, run_id=None, dataset_id=None,
                            max_charge_usd=.05, launch_allowed=True, save_run=AsyncMock(), reconcile_usage=AsyncMock())


async def _run(source_class, rows, task):
    requests: list[httpx.Request] = []
    adapter = source_class("secret-token", client=_api(rows, requests), sleep=AsyncMock())
    assert adapter.supports(task)
    with source_run_scope(_context(adapter.name)):
        listings = await adapter.search(task, limit=10)
    await adapter.aclose()
    body = json.loads(next(r for r in requests if r.method == "POST").content)
    return body, listings


def test_flag_is_off_by_default_and_reaches_the_worker_config(monkeypatch):
    monkeypatch.delenv("SEARCH_PLACE_LEVEL_FROM_SPEC", raising=False)
    assert WebSearchSettings(_env_file=None).config().place_level_from_spec is False
    monkeypatch.setenv("SEARCH_PLACE_LEVEL_FROM_SPEC", "true")
    assert WebSearchSettings(_env_file=None).config().place_level_from_spec is True


async def test_spec_level_wins_over_the_text_only_with_the_flag():
    campaign = await _campaign("province")
    assert "провинц" not in campaign.source_text
    assert query_task(campaign).place_level == "city"  # off: read from the text, as before
    assert query_task(campaign, spec_level=True).place_level == "province"
    city = await _campaign("city", text="участок в провинции Мадрид")
    assert query_task(city).place_level == "region"
    assert query_task(city, spec_level=True).place_level == "city"  # the spec is the priority


@pytest.mark.parametrize(("level", "location", "country", "text", "expected"), [
    ("region", "Madrid", "ES", "", "province"),  # Comunidad de Madrid is one province
    ("region", "Málaga", "ES", "вся провинция Малага", "province"),
    ("region", "Málaga", "ES", "участок в провинции Малаги", "province"),
    ("region", "Málaga", "ES", "Малагская провинция", "province"),
    ("region", "Málaga", "ES", "parcela en la provincia de Málaga", "province"),
    ("region", "Málaga", "ES", "Andalucía, Costa del Sol", "region"),  # more than the province: not guessed
    # «provincia» away from the place says nothing about it
    ("region", "Málaga", "ES", "участок у моря в Малаге, провинция не важна", "region"),
    ("region", "Málaga", "ES", "Costa del Sol, cualquier provincia", "region"),
    # two provinces named: neither of them alone
    ("region", "Valencia", "ES", "provincia de Alicante o Valencia", "region"),
    ("region", "Barcelona", "ES", "provincia de Barcelona y Girona", "region"),
    ("region", "Madrid", "ES", "Мадрид и Толедо", "region"),
    ("region", "Madrid", "ES", "вся провинция Мадрид", "province"),
    ("province", "Alicante", "ES", "", "province"),
    ("province", "Costa del Sol", "ES", "", "region"),  # no province of Spain
    ("city", "Madrid", "ES", "provincia de Madrid", "city"),
    ("region", "Kyiv", "UA", "", "region"),
])
def test_portal_place_level(level, location, country, text, expected):
    assert portal_place_level(level, location, country, text) == expected


async def test_flag_off_actor_inputs_are_as_before():
    task = query_task(await _campaign("province"))
    body, _ = await _run(ApifyFotocasaSource, [], task)
    assert body["location"] == "Madrid" and body["locationType"] == "municipality"
    body, listings = await _run(ApifyMilanunciosSource, live_rows("milanuncios", "madrid_solares"), task)
    assert body["province"] == "madrid"
    assert not any(FUENTIDUENA in item.url for item in listings)  # dropped as apify_invalid_listing
    body, _ = await _run(ApifyIdealistaPortalSource, [], task)
    assert body["location"] == "Madrid" and "locationType" not in body


async def test_flag_on_province_reaches_fotocasa_milanuncios_and_idealista():
    task = query_task(await _campaign("province"), spec_level=True)
    assert task.place_level == "province"

    other_town = {"level5Name": "Fuentidueña de Tajo", "level2Name": "Madrid"}
    body, listings = await _run(ApifyFotocasaSource, [fotocasa_row(location=other_town)], task)
    assert body["location"] == "Madrid" and body["locationType"] == "province"
    assert len(listings) == 1

    body, listings = await _run(ApifyMilanunciosSource, live_rows("milanuncios", "madrid_solares"), task)
    assert body["province"] == "madrid"
    plot = next(item for item in listings if FUENTIDUENA in item.url)
    assert plot.plot_m2 == 13715 and plot.area_m2 is None and plot.price == 985000

    # Idealista (igolaizola) takes a place name only; the province shows in the rows it keeps.
    body, listings = await _run(ApifyIdealistaPortalSource, [idealista_row(municipality="Fuentidueña de Tajo")], task)
    assert body["location"] == "Madrid" and "locationType" not in body
    assert len(listings) == 1


async def test_flag_on_a_region_of_one_province_is_a_province():
    task = query_task(await _campaign("region"), spec_level=True)
    assert task.place_level == "province"


def _axlymxp(rows, requests):
    return ApifyIdealistaSource("secret-token", location_id="configured-exact-city-id",
                                client=_api(rows, requests), sleep=AsyncMock())


async def test_flag_on_keeps_idealista_axlymxp_with_the_task_it_had_before():
    """axlymxp serves only a city: with a province from the spec it searches the city, as without the flag."""
    campaign = await _campaign("province")
    on = WebSearchWorker(None, None, None, None, None, config=WebSearchConfig(place_level_from_spec=True))
    off = WebSearchWorker(None, None, None, None, None, config=WebSearchConfig())
    requests: list[httpx.Request] = []
    source = _axlymxp([], requests)
    assert not source.supports(query_task(campaign, spec_level=True))
    task = on._source_task(campaign, source)
    assert task.place_level == "city" and task == off._source_task(campaign, source)
    fotocasa = ApifyFotocasaSource("secret-token", client=_api([], []), sleep=AsyncMock())
    assert on._source_task(campaign, fotocasa).place_level == "province"

    class Store:
        async def claim_source(self, cid, name):
            return object(), True

    on.store, on.sources = Store(), (source,)
    on._source_limit = AsyncMock(return_value=5)
    on._source_charge = AsyncMock(return_value=.05)
    on._search_source = AsyncMock()
    await on._from_sources(campaign)
    on._search_source.assert_awaited_once()  # the Idealista run is launched, not dropped

    with source_run_scope(_context(source.name)):
        await source.search(task, limit=5)
    await source.aclose()
    body = json.loads(next(r for r in requests if r.method == "POST").content)
    assert body["locationName"] == "Madrid" and body["locationId"] == "configured-exact-city-id"
