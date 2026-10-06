from __future__ import annotations

import httpx
import pytest

from bot.web_search.search_backends import GoogleCseClient, MergedSearcher, SerpApiClient
from bot.web_search.searxng import SearchError, SearchHit, SearxngClient
from bot.web_search.settings import WebSearchSettings


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _items(*urls):
    return [{"link": u, "title": f"t {u}", "snippet": "s"} for u in urls]


async def test_cse_paging_and_mapping():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        start = int(request.url.params["start"])
        if start == 1:
            return httpx.Response(200, json={"items": _items("https://a.es/1", "https://a.es/2")})
        if start == 11:
            return httpx.Response(200, json={"items": _items("https://a.es/3")})
        return httpx.Response(200, json={})

    cse = GoogleCseClient("KEY", "CX", max_results=30, timeout=5, client=_client(handler))
    hits = await cse.search("piso", language="es")
    assert [h.url for h in hits] == ["https://a.es/1", "https://a.es/2", "https://a.es/3"]
    assert hits[0] == SearchHit("https://a.es/1", "t https://a.es/1", "s", "google_cse")
    assert [p["start"] for p in seen] == ["1", "11", "21"]
    assert seen[0]["lr"] == "lang_es" and seen[0]["cx"] == "CX" and seen[0]["num"] == "10"


async def test_cse_quota_error_hides_key():
    cse = GoogleCseClient("SECRETKEY", "CX", max_results=10, timeout=5,
                          client=_client(lambda r: httpx.Response(429, json={})))
    with pytest.raises(SearchError) as err:
        await cse.search("q")
    assert err.value.code == "quota" and "SECRETKEY" not in str(err.value)
    cse403 = GoogleCseClient("K", "CX", max_results=10, timeout=5,
                             client=_client(lambda r: httpx.Response(403, json={})))
    with pytest.raises(SearchError, match="quota"):
        await cse403.search("q")


async def test_serpapi_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "google" and request.url.params["api_key"] == "K"
        return httpx.Response(200, json={"organic_results": [
            {"link": "https://b.es/x", "title": "T", "snippet": "S"}, {"title": "no link"}]})

    hits = await SerpApiClient("K", max_results=10, timeout=5, client=_client(handler)).search("q", language="es")
    assert hits == [SearchHit("https://b.es/x", "T", "S", "serpapi")]
    bad = SerpApiClient("K", client=_client(lambda r: httpx.Response(429)))
    with pytest.raises(SearchError, match="quota"):
        await bad.search("q")


class Fake:
    def __init__(self, name, urls=(), error=None):
        self.name, self.urls, self.error, self.calls = name, urls, error, 0

    async def search(self, query, *, language=None, pages=None):
        self.calls += 1
        if self.error:
            raise self.error
        return [SearchHit(u, engine=self.name) for u in self.urls]

    async def aclose(self):
        pass


async def test_merged_round_robin_dedup_and_cap():
    a = Fake("a", ["https://x.es/1?utm_source=z", "https://x.es/2", "https://x.es/3"])
    b = Fake("b", ["https://x.es/1", "https://y.es/1", "https://y.es/2"])
    hits = await MergedSearcher([a, b], max_results=4).search("q")
    assert [h.url for h in hits] == ["https://x.es/1?utm_source=z", "https://x.es/2", "https://y.es/1", "https://x.es/3"]


async def test_merged_one_backend_failing(caplog):
    good = Fake("good", ["https://x.es/1"])
    bad = Fake("bad", error=SearchError("quota"))
    merged = MergedSearcher([bad, good], max_results=10)
    with caplog.at_level("WARNING"):
        hits = await merged.search("q")
    assert [h.url for h in hits] == ["https://x.es/1"]
    assert "web_search.backend_failed bad" in caplog.text
    with pytest.raises(SearchError):
        await MergedSearcher([bad], max_results=10).search("q")


async def test_merged_daily_cap_skips_backend():
    a, b = Fake("a", ["https://x.es/1"]), Fake("b", ["https://y.es/1"])
    merged = MergedSearcher([a, b], max_results=10, daily_caps={"b": 1})
    await merged.search("q1")
    hits = await merged.search("q2")
    assert (a.calls, b.calls) == (2, 1)
    assert [h.url for h in hits] == ["https://x.es/1"]


def _settings(**kw) -> WebSearchSettings:
    return WebSearchSettings(_env_file=None, **kw)


def test_settings_searcher_selection():
    sx = SearxngClient("http://x")
    assert _settings().searcher(sx) is sx
    # key missing: skipped, searxng alone
    assert _settings(WEB_SEARCH_BACKENDS="searxng,google_cse").searcher(sx) is sx
    s = _settings(WEB_SEARCH_BACKENDS="searxng,google_cse,serpapi", GOOGLE_CSE_API_KEY="k", GOOGLE_CSE_CX="c",
                  SERPAPI_API_KEY="s")
    merged = s.searcher(sx)
    assert isinstance(merged, MergedSearcher) and len(merged.backends) == 3
    assert merged.daily_caps == {"google_cse": 90, "serpapi": 90}
    only = _settings(WEB_SEARCH_BACKENDS="serpapi", SERPAPI_API_KEY="s").searcher(sx)
    assert isinstance(only, MergedSearcher) and len(only.backends) == 1
