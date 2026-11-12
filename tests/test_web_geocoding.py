"""A radius search uses only an exact Spanish city centre that is cached first."""

import json
from unittest.mock import AsyncMock

import httpx

from bot.web_search.geocoding import CachedCityCenters


async def test_exact_city_center_is_cached_and_reused():
    cache = AsyncMock()
    cache.get.side_effect = [None, json.dumps([40.4168, -3.7038])]
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["city"] == "Madrid"
        assert request.url.params["countrycodes"] == "es"
        assert "real-estate-bot" in request.headers["user-agent"]
        return httpx.Response(200, json=[{"name": "Madrid", "lat": "40.4168", "lon": "-3.7038",
                                          "address": {"country_code": "es", "city": "Madrid"}}])

    resolver = CachedCityCenters("", cache=cache, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                 sleep=AsyncMock())
    assert await resolver.resolve("Madrid") == (40.4168, -3.7038)
    assert await resolver.resolve("Madrid") == (40.4168, -3.7038)
    assert len(requests) == 1
    cache.setex.assert_awaited_once()
    await resolver.aclose()


async def test_ambiguous_or_uncacheable_city_fails_closed():
    cache = AsyncMock()
    cache.get.return_value = None
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[
        {"name": "Madrid", "lat": "40.4", "lon": "-3.7", "address": {"country_code": "es", "city": "Madrid"}},
    ])))
    resolver = CachedCityCenters("", cache=cache, client=client, sleep=AsyncMock())
    assert await resolver.resolve("Barcelona") is None
    cache.setex.side_effect = ConnectionError("redis down")
    assert await resolver.resolve("Madrid") is None
    await resolver.aclose()


async def test_bounding_box_is_cached_next_to_the_centre_and_old_entries_have_none():
    cache = AsyncMock()
    cache.get.return_value = None

    def handler(request):
        return httpx.Response(200, json=[{"name": "València", "lat": "39.4699", "lon": "-0.3763",
                                          "boundingbox": ["39.278", "39.567", "-0.433", "-0.272"],
                                          "address": {"country_code": "es", "city": "València"}}])

    resolver = CachedCityCenters("", cache=cache, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                 sleep=AsyncMock())
    assert await resolver.resolve_area("Valencia") == ((39.4699, -0.3763), (39.278, 39.567, -0.433, -0.272))
    stored = json.loads(cache.setex.await_args.args[2])
    assert stored == [39.4699, -0.3763, [39.278, 39.567, -0.433, -0.272]]
    cache.get.return_value = json.dumps(stored)
    assert await resolver.resolve("Valencia") == (39.4699, -0.3763)
    cache.get.return_value = json.dumps([39.4699, -0.3763])  # written before the box
    assert await resolver.resolve_area("Valencia") == ((39.4699, -0.3763), None)
    assert await resolver.resolve("Valencia") == (39.4699, -0.3763)
    cache.get.return_value = json.dumps([39.4699, -0.3763, ["x", 1, 2, 3]])  # a broken box: the centre, no box
    assert await resolver.resolve_area("Valencia") == ((39.4699, -0.3763), None)
    await resolver.aclose()
