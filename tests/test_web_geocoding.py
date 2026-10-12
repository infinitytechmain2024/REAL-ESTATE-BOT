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
