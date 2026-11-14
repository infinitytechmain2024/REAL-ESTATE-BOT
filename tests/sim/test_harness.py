"""T1: the user simulation bench (``scripts/sim``) on synthetic cassettes, never a network.

The cassette and Apify tests need no database. The end-to-end ones run the whole task (interview -> campaign ->
cards) on a real PostgreSQL and are skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database
(the bench wipes it, then leaves an empty ``public``).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path

import httpx
import pytest

from scripts.sim import db
from scripts.sim.cassette import (
    SimNet,
    SimStop,
    digest,
    llm_request,
    mark_now,
    offline,
    page_key,
    web_request,
)
from scripts.sim.fixtures import answer as answer_apify
from scripts.sim.harness import _questions
from scripts.sim.web import answer as answer_web
from tests.sim import model

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = Path(__file__).resolve().parents[1] / "fixtures" / "portals_live"
URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
postgres = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                              reason="set SYSTEM_TEST_DATABASE_URL to a disposable *_test database")
SECRETS = {"OPENROUTER_API_KEY": "sk-or-v1-sim-secret-0123456789", "APIFY_TOKEN": "apify_api_simsecret0123456789",
           "TELEGRAM_TOKEN": "123456:sim-telegram-secret-0123", "ANTHROPIC_API_KEY": "sk-ant-sim-secret-0123"}
CHAT = {"model": "anthropic/claude-sonnet-4.5", "temperature": 0, "max_tokens": 100,
        "messages": [{"role": "system", "content": "Answer in JSON."}, {"role": "user", "content": "Plot 2519 m2?"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "r", "strict": True,
                                                                   "schema": {"type": "object"}}}}


@pytest.fixture(autouse=True)
def _no_repository_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The services' settings read ``.env`` from the working folder: never the repository's in these tests."""
    monkeypatch.chdir(tmp_path)


def files_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*") if p.is_file())


async def post(url: str, body: dict, token: str = SECRETS["OPENROUTER_API_KEY"]) -> httpx.Response:
    async with httpx.AsyncClient() as client:  # the default transport: answered by SimNet under ``offline``
        return await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})


# --- the LLM cassette ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recorded_answer_is_served_as_an_openrouter_completion_at_no_cost(tmp_path: Path) -> None:
    key = digest(llm_request("openrouter.ai", CHAT))
    (tmp_path / "cassette").mkdir()
    (tmp_path / "cassette" / f"{key}.json").write_text(json.dumps({"content": '{"area": 2519}'}), encoding="utf-8")
    with offline(SimNet(tmp_path)) as net:
        response = await post("https://openrouter.ai/api/v1/chat/completions", CHAT)
    body = response.json()
    assert response.status_code == 200 and body["choices"][0]["message"]["content"] == '{"area": 2519}'
    assert body["usage"]["cost"] == 0 and net.hits["llm"] == 1 and net.pending_llm == []


@pytest.mark.asyncio
async def test_a_new_request_is_pending_and_the_strict_run_stops(tmp_path: Path) -> None:
    with offline(SimNet(tmp_path, run_id="r1")) as net:
        response = await post("https://openrouter.ai/api/v1/chat/completions", CHAT)
    assert response.status_code == 503  # the bot goes on without it (more pending in one pass)
    [path] = (tmp_path / "pending").glob("*.json")
    pending = json.loads(path.read_text(encoding="utf-8"))
    assert pending["key"] == path.stem == net.pending_llm[0] and pending["run"] == "r1"
    assert pending["system"] == "Answer in JSON." and pending["user"] == "Plot 2519 m2?"
    assert pending["schema"] == {"type": "object"} and pending["answer_to"] == f"sim/cassette/{path.stem}.json"
    with offline(SimNet(tmp_path, strict=True)), pytest.raises(SimStop) as stop:
        await post("https://openrouter.ai/api/v1/chat/completions", CHAT)
    assert stop.value.kind == "llm" and stop.value.path == path


@pytest.mark.asyncio
async def test_the_anthropic_interviewer_gets_its_tool_call(tmp_path: Path) -> None:
    body = {"model": "claude-sonnet-5-5", "system": "Interview.", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "submit_turn", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "submit_turn"}}
    key = digest(llm_request("api.anthropic.com", body))
    (tmp_path / "cassette").mkdir()
    (tmp_path / "cassette" / f"{key}.json").write_text(json.dumps({"content": '{"done": true}'}), encoding="utf-8")
    with offline(SimNet(tmp_path)):
        response = await post("https://api.anthropic.com/v1/messages", body)
    [block] = response.json()["content"]
    assert block == {"type": "tool_use", "id": f"toolu_sim_{key}", "name": "submit_turn", "input": {"done": True}}


def test_the_key_ignores_ids_but_not_the_question() -> None:
    def chat(text: str) -> dict:
        return llm_request("openrouter.ai", {**CHAT, "messages": [{"role": "user", "content": text}]})

    assert digest(chat("run 6f1d2c1e-8f3b-4a59-9d7e-1c2b3a4d5e6f")) == \
        digest(chat("run 0a1b2c3d-0000-4000-8000-000000000001"))
    assert digest(chat("plot 2000 m2")) != digest(chat("plot 3000 m2"))


def test_a_listing_date_stays_in_the_key_and_the_benchs_now_does_not() -> None:
    from datetime import UTC, datetime, timedelta

    def chat(data: dict) -> dict:
        return llm_request("openrouter.ai", {**CHAT, "messages": [{"role": "user", "content": json.dumps(data)}]})

    assert digest(chat({"listing_date": "2026-10-01"})) != digest(chat({"listing_date": "2026-10-02"}))
    now = datetime.now(UTC)
    later = now + timedelta(days=3)  # the virtual clock got there
    mark_now(later)
    assert digest(chat({"asked_at": now.isoformat(), "today": now.date().isoformat()})) == \
        digest(chat({"asked_at": later.isoformat(), "today": later.date().isoformat()}))


# --- Apify ------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_apify_run_lives_its_cycle_from_the_record_and_a_miss_is_pending(tmp_path: Path) -> None:
    from bot.acquisition.apify_client import ApifyHttp, PermanentApifyError

    actor_input = {"location": "Madrid", "operation": "sale", "propertyType": "lands"}
    folder = tmp_path / "apify" / "igolaizola~idealista-scraper"
    folder.mkdir(parents=True)
    rows = json.loads((LIVE / "idealista_items.json").read_text(encoding="utf-8"))
    (folder / f"{digest(actor_input, 16)}.json").write_text(json.dumps(rows), encoding="utf-8")
    with offline(SimNet(tmp_path, run_id="r1")) as net:
        http = ApifyHttp(SECRETS["APIFY_TOKEN"])
        run = await http.launch("igolaizola/idealista-scraper", actor_input, max_items=20, max_charge_usd=.1,
                                timeout_seconds=60)
        assert run["status"] == "RUNNING"
        done = await http.run(run["id"])
        first = await http.dataset_page(done["defaultDatasetId"], offset=0, limit=2)
        rest = await http.dataset_page(done["defaultDatasetId"], offset=2, limit=100)
        with pytest.raises(PermanentApifyError):  # a 4xx: nothing was started, the campaign goes on
            await http.launch("igolaizola/idealista-scraper", {**actor_input, "location": "Bilbao"}, max_items=20,
                              max_charge_usd=.1, timeout_seconds=60)
        await http.aclose()
    assert done["status"] == "SUCCEEDED" and done["usageTotalUsd"] == 0.0 and first + rest == rows
    [pending] = (tmp_path / "pending_apify").glob("*.json")
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["actor"] == "igolaizola/idealista-scraper" and data["input"]["location"] == "Bilbao"
    assert data["params"]["maxTotalChargeUsd"] == "0.1" and net.pending_apify == [pending.stem]


@pytest.mark.asyncio
async def test_an_apify_record_gives_no_more_than_the_launch_asked_for(tmp_path: Path) -> None:
    from bot.acquisition.apify_client import ApifyHttp

    folder = tmp_path / "apify" / "rastriq~milanuncios-scraper"
    folder.mkdir(parents=True)
    (folder / "_default.json").write_text(json.dumps([{"id": n} for n in range(10)]), encoding="utf-8")
    with offline(SimNet(tmp_path)):
        http = ApifyHttp(SECRETS["APIFY_TOKEN"])
        run = await http.launch("rastriq/milanuncios-scraper", {"province": "madrid"}, max_items=3,
                                max_charge_usd=.1, timeout_seconds=60)
        done = await http.run(run["id"])
        items = await http.dataset_page(done["defaultDatasetId"], offset=0, limit=100)
        await http.aclose()
    assert items == [{"id": 0}, {"id": 1}, {"id": 2}]


def test_the_live_portal_fixtures_answer_the_runs_they_match(tmp_path: Path) -> None:
    folder = tmp_path / "pending_apify"
    folder.mkdir()
    for name, actor, actor_input in (
            ("a", "igolaizola/idealista-scraper", {"location": "Madrid", "operation": "sale", "propertyType": "lands"}),
            ("b", "rastriq/milanuncios-scraper", {"province": "madrid", "categorySlug": "solares"}),
            ("c", "igolaizola/fotocasa-scraper", {"location": "Madrid"})):
        (folder / f"{name}.json").write_text(json.dumps({"actor": actor, "key": name, "input": actor_input}))
    answered, left = answer_apify(tmp_path)
    assert len(answered) == 2 and left == ["c.json"]
    assert (tmp_path / "apify" / "rastriq~milanuncios-scraper" / "b.json").read_bytes() == \
        (LIVE / "milanuncios_items_madrid_solares.json").read_bytes()


def pending(tmp_path: Path, runs: tuple[tuple[str, str, dict[str, object]], ...]) -> None:
    folder = tmp_path / "pending_apify"
    folder.mkdir()
    for name, actor, actor_input in runs:
        (folder / f"{name}.json").write_text(json.dumps({"actor": actor, "key": name, "input": actor_input}))


def test_a_portal_fixture_answers_only_the_search_it_was_recorded_with(tmp_path: Path) -> None:
    # Q35, run b2-n4-s3: plots in the province of Madrid, up to 600 000 EUR, from 500 m2, as the bot sends them
    pending(tmp_path, (
        ("a", "igolaizola/idealista-scraper",
         {"maxItems": 20, "operation": "sale", "propertyType": "lands", "country": "es", "sortBy": "mostRecent",
          "location": "Madrid", "maxPrice": "600000", "minSize": "300"}),
        ("b", "trev0n/habitaclia-com-spain-scraper",
         {"transactionType": "sale", "propertyType": "land", "location": "Madrid", "sort": "newest",
          "maxListings": 20, "maxPages": 1, "fetchPropertyDetails": False, "maxPrice": 600000, "minArea": 500}),
        ("c", "logiover/pisos-com-property-scraper",
         {"transactionType": "sale", "propertyType": "land", "location": "madrid", "sortBy": "newest",
          "maxListings": 20, "maxPages": 1, "fetchDetails": False, "maxPrice": 600000, "minArea": 500}),
        ("d", "logiover/pisos-com-property-scraper",  # the province without filters: still another search
         {"transactionType": "sale", "propertyType": "land", "location": "madrid", "sortBy": "newest"}),
        ("e", "igolaizola/idealista-scraper",  # biggest first, or the city's circle: other searches
         {"operation": "sale", "propertyType": "lands", "location": "Madrid", "sortBy": "biggest"}),
        ("f", "igolaizola/idealista-scraper",
         {"operation": "sale", "propertyType": "lands", "location": "Madrid", "sortBy": "mostRecent",
          "latitude": 40.4168, "longitude": -3.7038, "distanceKm": 9}),
        ("g", "igolaizola/yaencontre-scraper",  # a province circle (60 km) / a filter
         {"operation": "sale", "propertyType": "land", "sortBy": "newest", "latitude": 40.4168,
          "longitude": -3.7038, "radiusKm": 60}),
        ("h", "igolaizola/yaencontre-scraper",
         {"operation": "sale", "propertyType": "land", "sortBy": "newest", "latitude": 40.4168,
          "longitude": -3.7038, "radiusKm": 15, "maxPrice": "600000"}),
        ("i", "igolaizola/yaencontre-scraper",  # another city
         {"operation": "sale", "propertyType": "land", "sortBy": "newest", "latitude": 41.3874,
          "longitude": 2.1686, "radiusKm": 15})))
    answered, left = answer_apify(tmp_path)
    assert answered == [] and left == [f"{n}.json" for n in "abcdefghi"]
    assert not (tmp_path / "apify").exists()


def test_a_portal_fixture_answers_the_input_it_was_recorded_with(tmp_path: Path) -> None:
    runs = {  # the bot's input for plots (flats) in the city of Madrid, no filters -> the fixture
        "igolaizola~idealista-scraper": ("a", {"maxItems": 20, "operation": "sale", "propertyType": "lands",
                                               "country": "es", "sortBy": "mostRecent", "location": "Madrid"},
                                         "idealista_items.json"),
        "trev0n~habitaclia-com-spain-scraper": ("b", {"transactionType": "sale", "propertyType": "land",
                                                      "location": "Madrid", "sort": "newest", "maxListings": 20},
                                                "habitaclia_items.json"),
        "logiover~pisos-com-property-scraper": ("c", {"transactionType": "sale", "propertyType": "land",
                                                      "location": "madrid_capital", "sortBy": "newest"},
                                                "pisos_items.json"),
        "igolaizola~yaencontre-scraper": ("d", {"maxItems": 20, "operation": "sale", "propertyType": "flat",
                                                "sortBy": "newest", "latitude": 40.4168, "longitude": -3.7038,
                                                "radiusKm": 15}, "yaencontre_flat_items.json"),
        "rastriq~milanuncios-scraper": ("e", {"categorySlug": "alquiler-de-pisos", "province": "madrid",
                                              "maxItems": 20}, "milanuncios_items_madrid_alquiler_pisos.json"),
    }
    pending(tmp_path, tuple((key, actor.replace("~", "/"), body) for actor, (key, body, _) in runs.items()))
    answered, left = answer_apify(tmp_path)
    assert left == [] and len(answered) == 5
    for actor, (key, _, name) in runs.items():
        assert (tmp_path / "apify" / actor / f"{key}.json").read_bytes() == (LIVE / name).read_bytes()


def test_a_portal_fixture_gives_only_the_rows_the_input_filters_let_through(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.sim import fixtures

    # a fixture recorded with a filter (none is today) still gets the actor's own filters applied
    monkeypatch.setitem(fixtures.RULES, "igolaizola~idealista-scraper", lambda d: "idealista_items.json")
    monkeypatch.setitem(fixtures.RULES, "logiover~pisos-com-property-scraper", lambda d: "pisos_items.json")
    pending(tmp_path, (
        ("a", "igolaizola/idealista-scraper",  # sizes 67, 179, 232 m2: only 232 is from 200
         {"location": "Madrid", "operation": "sale", "propertyType": "lands", "minSize": "200"}),
        ("c", "logiover/pisos-com-property-scraper",  # areas None, 1063, 232, 860; prices up to 2.1 M
         {"location": "madrid_capital", "transactionType": "sale", "propertyType": "land", "minArea": 800,
          "maxPrice": 2000000})))
    answered, left = answer_apify(tmp_path)
    assert left == [] and "1 of 3 rows" in answered[0] and "2 of 4 rows" in answered[1]
    rows = {n: json.loads((tmp_path / "apify" / a / f"{n}.json").read_text(encoding="utf-8"))
            for n, a in (("a", "igolaizola~idealista-scraper"), ("c", "logiover~pisos-com-property-scraper"))}
    assert [r["size"] for r in rows["a"]] == [232]
    # a row without the area is kept (the portal's handling is unknown), 232 m2 and 2.1 M are not
    assert [(r["areaSqm"], r["price"]) for r in rows["c"]] == [(None, 1550000), (1063, 590000)]


def test_a_fixture_with_no_row_left_after_the_filters_is_not_written(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.sim import fixtures

    monkeypatch.setitem(fixtures.RULES, "igolaizola~idealista-scraper", lambda d: "idealista_items.json")
    pending(tmp_path, (("b", "igolaizola/idealista-scraper",  # sizes 67, 179, 232 m2: none is from 300
                        {"location": "Madrid", "operation": "sale", "propertyType": "lands", "minSize": "300"}),))
    answered, left = answer_apify(tmp_path)
    assert answered == [] and len(left) == 1
    assert left[0].startswith("b.json (0 of 3 rows of tests/fixtures/portals_live/idealista_items.json")
    assert not (tmp_path / "apify").exists() and (tmp_path / "pending_apify" / "b.json").is_file()


# --- SearXNG ----------------------------------------------------------------------------------------------------

HITS = {"query": "inversores malaga", "results": [
    {"url": "https://example.org/inversores", "title": "Inversores", "content": "Fondo en Málaga", "engine": "x"}]}


def search_key(query: str, **params: object) -> str:
    return digest(web_request(httpx.QueryParams({"q": query, "format": "json", **params})), 16)


@pytest.mark.asyncio
async def test_a_recorded_search_answer_is_served_to_the_searxng_client(tmp_path: Path) -> None:
    from bot.web_search.searxng import SearxngClient

    key = search_key("inversores malaga", pageno=1, categories="general", language="es")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / f"{key}.json").write_text(json.dumps(HITS), encoding="utf-8")
    with offline(SimNet(tmp_path, run_id="r1")) as net:
        client = SearxngClient("http://searxng:8080")
        hits = await client.search("Inversores  Malaga", language="es")
        await client.aclose()
    assert [(h.url, h.title, h.snippet) for h in hits] == [("https://example.org/inversores", "Inversores",
                                                            "Fondo en Málaga")]
    assert net.hits["web"] == 1 and net.pending_web == [] and net.offline == []
    assert not (tmp_path / "pending_web").exists()


@pytest.mark.asyncio
async def test_a_search_without_an_answer_is_pending_and_the_strict_run_stops(tmp_path: Path) -> None:
    from bot.web_search.searxng import SearchError, SearxngClient

    with offline(SimNet(tmp_path, run_id="r1")) as net:
        client = SearxngClient("http://searxng:8080")
        with pytest.raises(SearchError, match="http_503"):  # the bot's search fails, the pass goes on
            await client.search("coinversores Málaga", language="es")
        with pytest.raises(SearchError):  # the same search again: still one pending
            await client.search("coinversores  Málaga", language="es")
        await client.aclose()
    [path] = (tmp_path / "pending_web").glob("*.json")
    pending = json.loads(path.read_text(encoding="utf-8"))
    assert net.pending_web == [path.stem] == [pending["key"]] and net.offline == []
    assert pending["query"] == "coinversores málaga" and pending["host"] == "searxng" and pending["run"] == "r1"
    assert pending["params"] == {"engines": None, "language": "es", "pageno": 1, "categories": ["general"]}
    assert pending["answer_to"] == f"sim/web/{path.stem}.json" and pending["speculative"] is False
    with offline(SimNet(tmp_path, strict=True)), pytest.raises(SimStop) as stop:
        client = SearxngClient("http://searxng:8080")
        try:
            await client.search("coinversores Málaga", language="es")
        finally:
            await client.aclose()
    assert stop.value.kind == "web" and stop.value.path == path


@pytest.mark.asyncio
async def test_a_search_after_an_unanswered_model_call_is_speculative(tmp_path: Path) -> None:
    from bot.web_search.searxng import SearchError, SearxngClient

    with offline(SimNet(tmp_path)):
        await post("https://openrouter.ai/api/v1/chat/completions", CHAT)
        client = SearxngClient("http://searxng:8080")
        with pytest.raises(SearchError):
            await client.search("family office Madrid")
        await client.aclose()
    [path] = (tmp_path / "pending_web").glob("*.json")
    assert json.loads(path.read_text(encoding="utf-8"))["speculative"] is True


def test_the_search_key_is_stable_and_follows_the_query_and_the_page() -> None:
    base = search_key("inversores Málaga", engines="google,bing", language="es", pageno=1)
    assert base == search_key("  Inversores   málaga ", engines="Bing, google", language="ES", pageno="1",
                              safesearch=0)
    assert base == search_key("inversores Málaga", engines="google,bing", language="es")  # page 1 by default
    assert base != search_key("inversores Málaga", engines="google,bing", language="es", pageno=2)
    assert base != search_key("inversores Sevilla", engines="google,bing", language="es", pageno=1)
    assert base != search_key("inversores Málaga", engines="google", language="es", pageno=1)


def test_an_answer_on_the_same_topic_is_reused_for_another_wording(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "pending_web").mkdir()
    params = {"engines": None, "language": "es", "pageno": 1, "categories": ["general"]}
    (tmp_path / "web" / "old.json").write_text(json.dumps(
        {**HITS, "request": {"query": "inversores málaga", "params": params}}), encoding="utf-8")
    for name, query, extra in (("a", "Inversores  Malaga", {"language": "es-ES"}), ("b", "inversores malaga", {"pageno": 2}),
                               ("c", "inversores sevilla", {}), ("d", "malaga inversores", {})):
        (tmp_path / "pending_web" / f"{name}.json").write_text(json.dumps(
            {"kind": "web", "key": name, "query": query, "params": {**params, **extra}}), encoding="utf-8")
    (tmp_path / "pending_web" / "e.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "pending_web" / "f.json").write_text(json.dumps({"query": "inversores málaga"}), encoding="utf-8")
    answered, left, broken = answer_web(tmp_path)  # a broken pending is skipped, the rest is answered
    assert answered == ["a.json <- web/old.json"] and left == ["b.json", "c.json", "d.json"]
    assert broken == ["e.json", "f.json"]
    reused = json.loads((tmp_path / "web" / "a.json").read_text(encoding="utf-8"))
    assert reused["results"] == HITS["results"] and reused["request"]["query"] == "Inversores  Malaga"
    assert not (tmp_path / "pending_web" / "a.json").exists()



@pytest.mark.parametrize(("asked", "other"), [
    ("piso 3 dormitorios 2 baños", "piso 2 dormitorios 3 baños"),
    ("inversores 1.5 millones", "inversores 5.1 millones"),
    ("inversores 1,5 millones", "inversores 1.5 millones"),
    ("inversores malaga -fondos", "inversores malaga fondos"),
    ('"family office" malaga', "family office malaga"),
    ("inversores site:linkedin.com", "inversores linkedin.com"),
])
def test_a_reuse_never_changes_the_meaning_of_the_query(asked: str, other: str) -> None:
    from scripts.sim.web import topic

    assert topic(asked, {}) != topic(other, {})
    assert topic(asked.upper(), {}) == topic(asked, {})  # case still folds


@pytest.mark.asyncio
async def test_a_recorded_failure_and_an_empty_search_are_served_as_recorded(tmp_path: Path) -> None:
    from bot.web_search.searxng import SearchError, SearxngClient

    (tmp_path / "web").mkdir()
    for query, entry in (("caido", {"error_status": 503}), ("nada", {"results": []})):
        key = search_key(query, pageno=1, categories="general")
        (tmp_path / "web" / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")
    with offline(SimNet(tmp_path)) as net:
        client = SearxngClient("http://searxng:8080")
        with pytest.raises(SearchError, match="http_503"):
            await client.search("caido")
        assert await client.search("nada") == []
        await client.aclose()
    assert net.hits["web"] == 2 and net.pending_web == [] and not (tmp_path / "pending_web").exists()


@pytest.mark.asyncio
async def test_only_the_searxng_host_is_a_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_SEARCH_SEARXNG_URL", raising=False)
    with offline(SimNet(tmp_path)) as net:
        async with httpx.AsyncClient() as client:
            geo = await client.get("https://nominatim.openstreetmap.org/search", params={"q": "Málaga"})
            site = await client.get("https://www.tiktok.com/search", params={"q": "x"})
    assert geo.status_code == site.status_code == 503 and net.pending_web == []
    assert [o["host"] for o in net.offline] == ["nominatim.openstreetmap.org", "www.tiktok.com"]
    assert not (tmp_path / "pending_web").exists()
    monkeypatch.setenv("WEB_SEARCH_SEARXNG_URL", "http://search.local:8888")  # the run's own SearXNG
    with offline(SimNet(tmp_path)) as net:
        async with httpx.AsyncClient() as client:
            await client.get("http://search.local:8888/search", params={"q": "x"})
            await client.get("http://searxng:8080/search", params={"q": "x"})
    assert len(net.pending_web) == 1 and [o["host"] for o in net.offline] == ["searxng"]



@pytest.mark.asyncio
async def test_a_served_search_and_a_recorded_failure_are_progress_of_the_pass(tmp_path: Path) -> None:
    from bot.web_search.searxng import SearchError, SearxngClient

    (tmp_path / "web").mkdir()
    for query, entry in (("caido", {"error_status": 503}), ("algo", HITS)):
        key = search_key(query, pageno=1, categories="general")
        (tmp_path / "web" / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")
    with offline(SimNet(tmp_path)) as net:
        client = SearxngClient("http://searxng:8080")
        with pytest.raises(SearchError):
            await client.search("caido")
        await client.search("algo")
        await client.aclose()
    assert net.calls == 2  # the loop's sign of progress (``Bench._progress``)


class _LoopPool:
    """The two questions ``Bench._loop`` asks the database: the campaign's state, is its web stage searching."""

    def __init__(self, campaign: dict) -> None:
        self.campaign = campaign
        self.clock: list[float] = []

    async def fetchval(self, sql: str, *_args: object) -> object:
        return self.campaign["searching"] if "web_search_runs" in sql else self.campaign["state"]

    async def execute(self, sql: str, *args: object) -> None:
        assert "sim.clock" in sql
        self.clock.append(float(args[0]))  # type: ignore[arg-type]


async def _loop(tmp_path: Path, rounds_searching: int | None) -> tuple[object, _LoopPool]:
    """``rounds_searching`` rounds of a web stage at work and no call, then the campaign ends (None: never)."""
    from types import SimpleNamespace

    from scripts.sim.harness import Bench, Persona

    campaign = {"state": "running", "searching": True}
    bench = Bench(tmp_path, "loop", Persona(name="t", task="t"))
    bench.pool, bench.campaign_id = _LoopPool(campaign), "c1"  # type: ignore[attr-defined]
    ticks = 0

    async def web() -> None:
        nonlocal ticks
        ticks += 1
        if rounds_searching is not None and ticks > rounds_searching:
            campaign.update(state="completed", searching=False)

    class Campaigns:
        async def get(self, _id: str) -> object:
            return SimpleNamespace(state=campaign["state"])

    await bench._loop([("web", web)], Campaigns(), None)
    return bench, bench.pool  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_searching_web_stage_is_no_idle_round_and_the_db_clock_follows(tmp_path: Path) -> None:
    from scripts.sim.harness import MAX_JUMPS

    bench, pool = await _loop(tmp_path, 3 * MAX_JUMPS)  # idle rounds would have stalled the run by now
    assert bench.status == "running" and bench.stop_reason == ""  # ended, not «did not end»
    assert pool.clock and pool.clock[-1] == bench.clock.offset.total_seconds()  # the DB's now() moved with it


@pytest.mark.asyncio
async def test_a_web_stage_that_never_ends_still_stalls_the_run(tmp_path: Path) -> None:
    from scripts.sim.harness import IDLE_JUMP, MAX_BUSY_ROUNDS, MAX_JUMPS

    bench, _ = await _loop(tmp_path, None)
    assert bench.status == "stalled" and bench.stop_reason == "the campaign did not end"
    assert bench.clock.offset.total_seconds() <= MAX_BUSY_ROUNDS * 10 + MAX_JUMPS * IDLE_JUMP.total_seconds()


@postgres
@pytest.mark.asyncio
async def test_the_databases_now_is_the_runners_virtual_clock(tmp_path: Path) -> None:
    from datetime import timedelta

    import asyncpg

    url = await db.reset(URL)
    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    try:
        await db.set_clock(pool, timedelta(hours=4))
        campaign = await pool.fetchval("insert into campaigns(telegram_chat_id, requested_by, source_text, plan, state) "
                                       "values (1, 1, 't', '{}'::jsonb, 'running') returning id")
        await pool.execute("update campaigns set state = 'completed' where id = $1", campaign)
        shift = await pool.fetchval("select finished_at - pg_catalog.now() from campaigns where id = $1", campaign)
        assert timedelta(hours=3, minutes=59) < shift <= timedelta(hours=4, minutes=1)  # the trigger's now()
    finally:
        await pool.close()
        await db.restore(URL)


# --- the interview's screen --------------------------------------------------------------------------------------


def test_the_bench_runs_with_the_production_card_fields() -> None:
    from scripts.sim.harness import BASE_ENV

    assert BASE_ENV["CARD_EXTRA_FIELDS"] == "true"


def test_the_bottom_keyboard_reaches_the_dialogue_and_the_pending_and_the_helper_presses_it(tmp_path: Path) -> None:
    from bot.control_plane.intake import TASK_KEYBOARD
    from bot.control_plane.models import Button, Reply
    from scripts.sim import answer_user
    from scripts.sim.harness import AnswerStop, Bench, Persona

    bench = Bench(tmp_path, "kb", Persona(name="p", task="участок"))
    bench._say("bot", Reply("Какой бюджет?", (Button("Не важно", callback_data="task:skip"),)))
    card = Reply("Проверьте задачу:\nучасток, Мадрид", keyboard=TASK_KEYBOARD)
    bench._say("bot", card)
    bench._say("bot", Reply("Уведомление"))  # no keyboard in the reply: the one on screen stays
    with pytest.raises(AnswerStop) as stop:
        bench._ask(card, card=True)
    pending = json.loads(stop.value.path.read_text(encoding="utf-8"))
    keys = [["▶️ Запустить", "✏️ Изменить"], ["❌ Отмена"]]
    assert pending["keyboard"] == keys and pending["buttons"] == []
    assert [d.get("keyboard") for d in bench.dialogue] == [None, keys, None]
    persona = {"replies": {"card": [{"press": "Изменить", "weight": 1}]}}
    assert answer_user.answer_pending(tmp_path, "kb", persona, 0)[0] == answer_user.EXIT_ANSWERED
    assert json.loads((tmp_path / "answers" / "kb.json").read_text(encoding="utf-8"))["answers"] == \
        [{"press": "Изменить"}]
    assert answer_user.keyboard_label(pending["keyboard"], "Изменить") == "✏️ Изменить"
    assert answer_user.keyboard_label(pending["keyboard"], "❌ Отмена") == "❌ Отмена"
    assert answer_user.keyboard_label(pending["keyboard"], "Остановить поиск") is None
    bench._say("bot", Reply("Черновик удалён.", keyboard=()))  # the keyboard removed: nothing to press
    assert bench.keyboard == [] and bench.dialogue[-1]["keyboard"] == []


def test_a_reply_with_inline_buttons_does_not_change_the_bottom_keyboard(tmp_path: Path) -> None:
    from bot.control_plane.intake import TASK_KEYBOARD
    from bot.control_plane.models import Button, Reply
    from scripts.sim.harness import Bench, Persona

    bench = Bench(tmp_path, "kb2", Persona(name="p", task="участок"))
    bench._say("bot", Reply("Проверьте задачу:\nучасток, Мадрид", keyboard=TASK_KEYBOARD))
    shown = bench.keyboard
    # prod sends only the inline buttons (bot/control_plane/main.py:_markup): the user never sees this keyboard
    bench._say("bot", Reply("Какой бюджет?", (Button("Не важно", callback_data="task:skip"),), keyboard=()))
    assert bench.keyboard == shown
    assert "keyboard" not in bench.dialogue[-1]


# --- the runner's «Показать?» after the cards ---------------------------------------------------------------------


class _Control:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def handle_callback(self, user_id: int, data: str, *, chat_id: int):
        from bot.control_plane.models import Reply

        self.pressed.append(data)
        return Reply("Хорошо, пришлю похожие варианты.")


def _offer_bench(root: Path):
    from scripts.sim.harness import USER, Bench, Persona

    bench = Bench(root, "near", Persona(name="p", task="дом в Марбелье"))
    bench.control = _Control()
    campaign = "00000000-0000-4000-8000-00000000000b"
    bench.net.telegram.messages += [
        {"message_id": 1001, "chat_id": USER, "text": "Карточка 1", "buttons": [], "edits": 0, "deleted": False},
        {"message_id": 1002, "chat_id": USER, "text": "Есть ещё похожие варианты (таунхаус). Показать?",
         "buttons": [{"text": "Одобрить", "data": f"near:yes:similar:{campaign}"},
                     {"text": "Нет", "data": f"near:no:similar:{campaign}"}], "edits": 0, "deleted": False}]
    return bench


@pytest.mark.asyncio
async def test_the_show_similar_question_waits_for_the_user_and_the_answer_presses_it(tmp_path: Path) -> None:
    from scripts.sim import answer_user
    from scripts.sim.harness import AnswerStop

    bench = _offer_bench(tmp_path)
    with pytest.raises(AnswerStop) as stop:  # nothing answered yet: the run waits, like an interview question
        await bench._offer()
    pending = json.loads(stop.value.path.read_text(encoding="utf-8"))
    assert pending["offer"] is True and pending["index"] == 0 and pending["question"].endswith("Показать?")
    assert [b["text"] for b in pending["buttons"]] == ["Одобрить", "Нет"]
    assert bench.dialogue[-1]["source"] == "offer"
    persona = {"replies": {"near": [{"press": "Одобрить", "weight": 1}]}}
    assert answer_user.answer_pending(tmp_path, "near", persona, 0)[0] == answer_user.EXIT_ANSWERED

    again = _offer_bench(tmp_path)  # the next pass replays the answer
    assert await again._offer() is True
    assert again.control.pressed == ["near:yes:similar:00000000-0000-4000-8000-00000000000b"]
    assert again.offers == [{"question": "Есть ещё похожие варианты (таунхаус). Показать?", "index": 0, "tag": None,
                             "press": "near:yes:similar:00000000-0000-4000-8000-00000000000b", "bucket": "similar"}]
    assert {"question": "Есть ещё похожие варианты (таунхаус). Показать?",
            "answer": "[near:yes:similar:00000000-0000-4000-8000-00000000000b]"} in _questions(again.dialogue)
    assert await again._offer() is False  # asked once


@pytest.mark.asyncio
async def test_without_a_near_reply_the_answer_is_no_and_the_result_says_so(tmp_path: Path) -> None:
    from scripts.sim import answer_user
    from scripts.sim.harness import AnswerStop

    with pytest.raises(AnswerStop):
        await _offer_bench(tmp_path)._offer()
    assert answer_user.answer_pending(tmp_path, "near", {"replies": {}}, 0)[0] == answer_user.EXIT_ANSWERED
    bench = _offer_bench(tmp_path)
    await bench._offer()
    assert bench.control.pressed == ["near:no:similar:00000000-0000-4000-8000-00000000000b"]
    assert bench.offers[0]["tag"] == "default"
    result = await bench.result()  # no pool: the dialogue part only
    assert result["offers"] == bench.offers


@pytest.mark.asyncio
async def test_a_missing_model_answer_lets_the_pass_go_on_before_the_question(tmp_path: Path) -> None:
    bench = _offer_bench(tmp_path)
    bench.net.pending_llm.append("abc")
    assert await bench._offer() is False  # collected first: the question waits for the next pass
    assert not (tmp_path / "pending_answers" / "near.json").exists() and bench.offers == []


class _Interview:
    """The control plane's draft for ``Bench._talk``: one question (the budget), then «Проверьте задачу»."""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.step = "budget"
        self.intake = SimpleNamespace(store=self)
        self.said: list[str] = []

    async def get(self, user_id: int):
        from types import SimpleNamespace

        return None if self.step == "launched" else SimpleNamespace(step=self.step)

    async def text(self, body: str):
        from bot.control_plane.models import Reply

        self.said.append(body)
        self.step = "summary"
        return Reply("Проверьте задачу:\nдом в Марбелье, до 1,5 млн €")

    async def press(self, data: str):
        from bot.control_plane.models import Reply

        self.said.append(f"[{data}]")
        if data == "task:launch":
            self.step = "launched"
        return Reply("Принято. Начинаю поиск.")


@pytest.mark.asyncio
async def test_a_card_launched_as_shown_leaves_the_next_answer_to_show_similar(tmp_path: Path) -> None:
    from bot.control_plane.models import Reply

    (tmp_path / "answers").mkdir()
    (tmp_path / "answers" / "near.json").write_text(json.dumps(
        {"answers": [{"text": "до 1,5 млн €"}, {"press": "Одобрить"}]}, ensure_ascii=False), encoding="utf-8")
    bench = _offer_bench(tmp_path)
    assert bench.persona.confirm_card is False
    interview = _Interview()
    launched = await bench._talk(interview, Reply("Какой бюджет?"), interview.text, interview.press, interview.press)
    assert launched and interview.said == ["до 1,5 млн €", "[task:launch]"]
    assert bench.answers_used == 1 and bench.check_card == "Проверьте задачу:\nдом в Марбелье, до 1,5 млн €"
    assert await bench._offer() is True  # answer 1 is the one for «Показать?», not eaten by the card
    assert bench.control.pressed == ["near:yes:similar:00000000-0000-4000-8000-00000000000b"]
    assert bench.offers[0]["index"] == 1 and bench.offers[0]["bucket"] == "similar"


@pytest.mark.asyncio
@pytest.mark.parametrize("wanted", ["task:dev:0", "Только точные", "нет, только точные", "task:opt:3"])
async def test_a_persona_press_goes_as_the_callback_of_the_button_it_means(tmp_path: Path, wanted: str) -> None:
    """Q24: the AI interviewer labels the deviation buttons itself; the persona's data (or words) is sent as the
    callback of that button, as the user's tap in Telegram."""
    from bot.control_plane.models import Button, Reply

    (tmp_path / "answers").mkdir()
    (tmp_path / "answers" / "near.json").write_text(json.dumps({"answers": [{"press": wanted}]}, ensure_ascii=False),
                                                     encoding="utf-8")
    bench = _offer_bench(tmp_path)
    interview = _Interview()

    async def press(data: str) -> Reply:
        if data != "task:launch":  # the deviation answered: the card
            interview.said.append(f"[{data}]")
            interview.step = "summary"
            return Reply("Проверьте задачу:\nквартира в Мадриде")
        return await interview.press(data)

    question = Reply("Если точных вариантов не будет, что допустимо?",
                     (Button("бюджет до +10 % (до 1320 €)", callback_data="task:opt:0"),
                      Button("нет, только точные", callback_data="task:opt:3"),
                      Button("Не важно", callback_data="task:skip")))
    assert await bench._talk(interview, question, interview.text, press, press)
    assert interview.said == ["[task:opt:3]", "[task:launch]"]


def test_a_manual_meaning_press_goes_through_the_callback_or_is_the_runs_error(tmp_path: Path) -> None:
    from bot.control_plane.models import Button, Reply
    from scripts.sim import answer_user
    from scripts.sim.harness import PressError, _callback

    (tmp_path / "pending_answers").mkdir()
    (tmp_path / "pending_answers" / "r.json").write_text(json.dumps({"index": 0, "question": "Что допустимо?"}),
                                                          encoding="utf-8")
    assert answer_user.main(["--run", "r", "--persona", str(_persona_file(tmp_path)), "--sim", str(tmp_path),
                             "--manual-press", "task:dev:10"]) == answer_user.EXIT_ANSWERED
    [answer] = json.loads((tmp_path / "answers" / "r.json").read_text(encoding="utf-8"))["answers"]
    assert answer == {"press": "task:dev:10"}  # as typed; the harness finds its button
    on_all = Reply("Что допустимо?", (Button("10 % по бюджету и площади", callback_data="task:opt:0"),
                                       Button("нет, только точные", callback_data="task:opt:1")))
    assert _callback(on_all, answer["press"]) == "task:opt:0"
    for buttons in ((Button("бюджет до +10 %", callback_data="task:opt:0"),),  # one tolerance only
                    (Button("±10 %", callback_data="task:opt:0"),
                     Button("10 % по бюджету и площади", callback_data="task:opt:1"))):  # two mean it
        with pytest.raises(PressError, match="task:dev:10"):
            _callback(Reply("Что допустимо?", buttons), answer["press"])
    assert _callback(Reply("?", (Button("не только точные", callback_data="task:opt:0"),
                                 Button("только точные", callback_data="task:opt:1"))), "task:dev:0") == "task:opt:1"


def _persona_file(root: Path) -> Path:
    path = root / "p.json"
    path.write_text(json.dumps({"name": "p", "replies": {}}), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_a_press_of_no_button_on_screen_is_the_runs_error(tmp_path: Path) -> None:
    from scripts.sim.harness import PressError

    (tmp_path / "answers").mkdir()
    (tmp_path / "answers" / "near.json").write_text(json.dumps({"answers": [{"press": "task:launch"}]}),
                                                     encoding="utf-8")
    bench = _offer_bench(tmp_path)
    with pytest.raises(PressError, match="task:launch"):  # ``Bench._run`` turns it into status error, code 4
        await bench._offer()
    assert bench.control.pressed == []


# --- the rest of the world ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_is_kept_and_nothing_else_leaves_the_process(tmp_path: Path) -> None:
    from bot.campaign.runner import TelegramMessenger

    with offline(SimNet(tmp_path)) as net:
        telegram = TelegramMessenger(SECRETS["TELEGRAM_TOKEN"])
        message = await telegram.send(1001, "Ищу…")
        await telegram.edit(1001, message, "Готово")
        await telegram.aclose()
        site = await post("https://www.idealista.com/inmueble/1/", {})
        with pytest.raises(OSError, match="no network"), socket.create_connection(("192.0.2.1", 443), timeout=1):
            pass
    [sent] = net.telegram.messages
    assert sent["text"] == "Готово" and sent["edits"] == 1 and site.status_code == 503
    assert net.offline[0]["host"] == "www.idealista.com"
    assert SECRETS["TELEGRAM_TOKEN"] not in json.dumps(net.offline + net.telegram.messages)


@pytest.mark.asyncio
async def test_an_impersonating_fetcher_reads_through_simnet_not_curl(tmp_path: Path) -> None:
    from bot.web_search.fetcher import CurlTransport, PageFetcher

    with offline(SimNet(tmp_path)) as net:
        fetcher = PageFetcher(user_agent="sim-test", impersonate="chrome", host_interval_seconds=0)
        assert fetcher.profile and not isinstance(fetcher._transport, CurlTransport)
        async with fetcher._transport.open("https://www.idealista.com/inmueble/1/", headers={}, proxy=None) as page:
            assert page.status == 503
        await fetcher.aclose()
    assert any(o["host"] == "www.idealista.com" for o in net.offline)



def _no_dns(*_args: object, **_kwargs: object) -> object:
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")  # the bench's container: no DNS


@pytest.mark.asyncio
async def test_the_fetcher_resolves_without_dns_and_reaches_simnet(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.web_search.fetcher import FetchError, PageFetcher, resolve_public_addresses

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    url = "https://www.inversores-malaga.es/club/"
    (tmp_path / "page").mkdir()
    (tmp_path / "page" / f"{page_key(url)}.json").write_text(json.dumps(
        {"status": 200, "content_type": "text/html", "body": "<html><body>Club de inversores</body></html>"}),
        encoding="utf-8")
    with offline(SimNet(tmp_path)) as net:
        fetcher = PageFetcher(user_agent="sim-test", impersonate="off", host_interval_seconds=0)
        assert await fetcher.allowed(url)  # robots.txt reached SimNet: no answer, a 404, every path allowed
        page = await fetcher.fetch(url)
        with pytest.raises(FetchError, match="private_target_forbidden"):  # an address literal is still checked
            await fetcher.fetch("http://10.0.0.1/")
        await fetcher.aclose()
    assert "Club de inversores" in page.html and net.hits["page"] == 1 and net.offline == []
    assert PageFetcher.__init__.__kwdefaults__["resolver"] is resolve_public_addresses  # given back
    fetcher = PageFetcher(user_agent="sim-test", impersonate="off")
    with pytest.raises(FetchError, match="dns_failed"):  # outside the bench the real resolver is used again
        await fetcher.fetch(url)
    await fetcher.aclose()


@pytest.mark.asyncio
async def test_a_page_without_an_answer_is_pending_and_robots_txt_is_absent(tmp_path: Path) -> None:
    from bot.web_search.fetcher import FetchError, PageFetcher

    url = "https://inversores.example.es/socios?b=2&a=1#equipo"
    with offline(SimNet(tmp_path, run_id="r1")) as net:
        fetcher = PageFetcher(user_agent="sim-test", impersonate="off", host_interval_seconds=0)
        assert await fetcher.allowed(url)
        for _ in range(2):  # the same page twice in a pass: one pending
            with pytest.raises(FetchError, match="network_error:ConnectError"):
                await fetcher.fetch(url)
        await fetcher.aclose()
    [path] = (tmp_path / "pending_page").glob("*.json")
    pending = json.loads(path.read_text(encoding="utf-8"))
    assert net.pending_page == [path.stem] == [pending["key"]] == [page_key(url)] and net.offline == []
    assert pending["url"] == url.split("#")[0] and pending["host"] == "inversores.example.es"
    assert pending["path"] == "/socios" and pending["run"] == "r1" and pending["speculative"] is False
    assert pending["answer_to"] == f"sim/page/{path.stem}.json"
    with offline(SimNet(tmp_path, strict=True)), pytest.raises(SimStop) as stop:
        fetcher = PageFetcher(user_agent="sim-test", impersonate="off", host_interval_seconds=0)
        await fetcher.fetch(url)
    assert stop.value.kind == "page"


@pytest.mark.asyncio
async def test_a_recorded_robots_txt_and_a_missing_page_are_served(tmp_path: Path) -> None:
    from bot.web_search.fetcher import FetchError, PageFetcher

    (tmp_path / "page").mkdir()
    (tmp_path / "page" / f"{page_key('https://club.example.es/robots.txt')}.json").write_text(json.dumps(
        {"status": 200, "content_type": "text/plain", "body": "User-agent: *\nDisallow: /privado/"}), encoding="utf-8")
    (tmp_path / "page" / f"{page_key('https://club.example.es/gone')}.json").write_text(json.dumps({"status": 404}),
                                                                                        encoding="utf-8")
    with offline(SimNet(tmp_path)) as net:
        fetcher = PageFetcher(user_agent="sim-test", impersonate="off", host_interval_seconds=0)
        assert not await fetcher.allowed("https://club.example.es/privado/1")
        assert await fetcher.allowed("https://club.example.es/socios")
        with pytest.raises(FetchError, match="http_404"):
            await fetcher.fetch("https://club.example.es/gone")
        await fetcher.aclose()
    assert net.hits["page"] == 2 and net.pending_page == [] and not (tmp_path / "pending_page").exists()



@pytest.mark.asyncio
async def test_page_misses_are_no_refusal_of_the_site_every_one_is_pending(tmp_path: Path) -> None:
    from bot.web_search import store, worker
    from bot.web_search.fetcher import FetchError, PageFetcher

    urls = [f"https://agencia.example.es/inmueble/{n}/" for n in range(5)]
    errors = []
    with offline(SimNet(tmp_path, run_id="r1")) as net:
        fetcher = PageFetcher(user_agent="sim-test", impersonate="off", host_interval_seconds=0)
        for url in urls:
            with pytest.raises(FetchError) as failed:
                await fetcher.fetch(url)
            errors.append(failed.value.code)
        await fetcher.aclose()
    assert sorted(net.pending_page) == sorted(page_key(u) for u in urls) and len(net.pending_page) == 5
    assert len(list((tmp_path / "pending_page").glob("*.json"))) == 5
    for code in set(errors):  # not a 503: no host breaker, no layer block, no render/scrape layer
        assert code == "network_error:ConnectError"
        assert code not in worker.BREAKER_ERRORS and code not in store._REFUSALS and code not in worker.RENDER_ON


@pytest.mark.asyncio
async def test_a_recorded_redirect_is_followed_to_its_own_page(tmp_path: Path) -> None:
    from bot.web_search.fetcher import PageFetcher

    (tmp_path / "page").mkdir()
    (tmp_path / "page" / f"{page_key('https://club.example.es/old')}.json").write_text(json.dumps(
        {"status": 301, "location": "/nuevo"}), encoding="utf-8")
    (tmp_path / "page" / f"{page_key('https://club.example.es/nuevo')}.json").write_text(json.dumps(
        {"status": 200, "content_type": "text/html", "body": "<p>Fondo</p>"}), encoding="utf-8")
    with offline(SimNet(tmp_path)) as net:
        fetcher = PageFetcher(user_agent="sim-test", impersonate="off", host_interval_seconds=0)
        page = await fetcher.fetch("https://club.example.es/old")
        await fetcher.aclose()
    assert page.url == "https://club.example.es/nuevo" and "Fondo" in page.html and net.hits["page"] == 2

def test_the_page_key_is_the_normalised_url() -> None:
    key = page_key("https://www.Example.es/a/?b=2&a=1#x")
    assert key == page_key("https://example.es/a?a=1&b=2") == page_key("http://example.es/a?a=1&b=2&utm_source=q")
    assert key != page_key("https://example.es/b?a=1&b=2") and len(key) == 16

def test_the_persona_cannot_turn_curl_on_and_the_process_is_given_back(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    from bot.utils import costs
    from scripts.sim.harness import Persona, environment, sim_process

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("all_proxy", "socks5://proxy.example:1080")
    persona = Persona(name="p", task="x", env={"WEB_SEARCH_IMPERSONATE": "chrome", "APIFY_TOKEN": "t"})
    env = environment(persona, "postgresql://x/sim_test")
    assert env["WEB_SEARCH_IMPERSONATE"] == "off" and env["APIFY_TOKEN"] == "t"
    random.seed(42)
    expected = random.getstate()
    ledger = costs.MemoryLedger()
    costs.install(ledger, 3.0)
    cwd = os.getcwd()
    try:
        with sim_process(tmp_path / "sim", env):
            assert "HTTPS_PROXY" not in os.environ and "all_proxy" not in os.environ
            assert os.environ["WEB_SEARCH_IMPERSONATE"] == "off" and os.getcwd() == str(tmp_path / "sim")
            random.random()
            costs.install(None)
        assert os.environ["HTTPS_PROXY"] == "http://proxy.example:3128"
        assert os.environ["all_proxy"] == "socks5://proxy.example:1080"  # noqa: SIM112 - the lower-case form is read too
        assert random.getstate() == expected and costs.ledger() is ledger and costs.budget() == 3.0
        assert os.getcwd() == cwd and "WEB_SEARCH_IMPERSONATE" not in os.environ
    finally:
        costs.install(None)


def test_the_spend_journal_sums_and_refuses_past_the_ceiling(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from scripts.sim.spend import main, record_spend, total

    record_spend("apify/instagram-scraper", "run1", 4.5, "G7", sim_root=tmp_path)
    assert main(["--sim", str(tmp_path), "add", "--actor", "harvestapi/linkedin-profile-search", "--run-id", "run2",
                 "--usd", "5.5"]) == 0
    assert total(tmp_path) == 10.0 and "total: $10.0000 of $10.00 (2 runs)" in capsys.readouterr().out
    record_spend("apify/instagram-scraper", "run3", 0.01, sim_root=tmp_path)
    assert main(["--sim", str(tmp_path)]) == 1
    with pytest.raises(ValueError):
        record_spend("a", "r", -1, sim_root=tmp_path)


# --- the whole run (PostgreSQL) ---------------------------------------------------------------------------------


def sim_root(tmp_path: Path) -> Path:
    """A bench folder with the synthetic task: the user's answer and the portals' live fixtures (``_default``)."""
    root = tmp_path / "sim"
    shutil.copytree(FIXTURES / "answers", root / "answers")
    for actor, name in json.loads((FIXTURES / "apify.json").read_text(encoding="utf-8")).items():
        (root / "apify" / actor).mkdir(parents=True)
        target = root / "apify" / actor / "_default.json"
        if name:
            shutil.copy(LIVE / name, target)
        else:
            target.write_text("[]", encoding="utf-8")
    return root


async def run(root: Path, *, responder=None, run_id: str = "land", confirm_card: bool = False) -> tuple[int, dict]:
    from scripts.sim.harness import Bench, Persona

    persona = Persona.load(str(FIXTURES / "personas" / "land_madrid.json"))
    persona.env.update(SECRETS)
    persona.confirm_card = confirm_card
    try:
        return await Bench(root, run_id, persona, database_url=URL, responder=responder).run()
    finally:
        await db.restore(URL)


@postgres
@pytest.mark.asyncio
async def test_a_land_search_in_madrid_reaches_a_card_and_a_replay_gives_the_same_result(tmp_path: Path) -> None:
    root = sim_root(tmp_path)
    code, first = await run(root, responder=model.answer)  # the synthetic model answers and fills the cassette
    assert code == 0 and first["status"] == "done", first["stop_reason"]
    [card] = first["cards"]
    assert card["url"] == "https://www.habitaclia.com/i15441000024727.htm"
    assert "1 550 000 €" in card["card"] and "habitaclia.com/i15441000024727" in card["card"]
    assert first["check_card"].startswith("Проверьте задачу:") and "от 2000 м²" in first["check_card"]
    assert first["campaign"]["state"] == "completed" and any("Отчёт по поиску" in r for r in first["report"])
    answers = [q["answer"] for q in first["questions"]]
    start = answers.index("до 3 млн евро")
    assert answers[start:start + 2] == ["до 3 млн евро", "[task:launch]"]
    # «Показать?» after the card (similar, then the farther ones), if the run held any: the fixture says «Нет»
    assert all(a.startswith("[near:no:") for a in answers[start + 2:]) and len(first["offers"]) <= 2
    assert [o["press"] for o in first["offers"]] == [a.strip("[]") for a in answers[start + 2:]]
    assert {c["provider"] for c in first["campaign_costs"] if c["stage"] == "llm" and c["kind"] == "cost"} == \
        {"claude_cassette"} and first["costs_total_usd"] == 0
    assert first["pending"] == {"llm": [], "apify": [], "apify_blocked": [], "web": [], "page": []}

    code, again = await run(root)  # the cassette alone, no model: the same file
    assert code == 0 and again == first
    text = files_text(root)
    assert not any(secret in text for secret in SECRETS.values())


@postgres
@pytest.mark.asyncio
async def test_without_answers_the_run_waits_with_pending_files_and_no_secret_in_them(tmp_path: Path) -> None:
    root = sim_root(tmp_path)
    code, result = await run(root, run_id="empty")
    assert code == 3 and result["status"] == "waiting"
    [pending] = (root / "pending").glob("*.json")  # the interview's first call: nothing goes on without it
    assert json.loads(pending.read_text(encoding="utf-8"))["stage"] == "interview"
    assert result["pending"]["llm"] == [pending.stem] and result["stop_reason"].startswith("llm:")
    text = files_text(root)
    assert not any(secret in text for secret in SECRETS.values())


@postgres
@pytest.mark.asyncio
async def test_the_card_is_launched_from_the_bottom_keyboard(tmp_path: Path) -> None:
    root = sim_root(tmp_path)
    (root / "answers" / "land.json").write_text(json.dumps(
        {"answers": [{"text": "до 3 млн евро"}, {"press": "▶️ Запустить"}, {"press": "Нет"}, {"press": "Нет"}]},
        ensure_ascii=False), encoding="utf-8")
    code, result = await run(root, responder=model.answer, confirm_card=True)  # the card reads its answer
    assert code == 0 and result["status"] == "done", result["stop_reason"]
    shown = next(d for d in result["dialogue"] if d["from"] == "bot" and d["text"].startswith("Проверьте задачу:"))
    assert shown["keyboard"] == [["▶️ Запустить", "✏️ Изменить"], ["❌ Отмена"]]
    assert {"from": "user", "text": "▶️ Запустить", "press": "▶️ Запустить"} in result["dialogue"]
    assert result["campaign"]["state"] == "completed"
    [card] = result["cards"]
    assert "Площадь: 2 519 м² (участок)" in card["card"]  # CARD_EXTRA_FIELDS=true, as in prod
