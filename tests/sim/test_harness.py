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
from scripts.sim.cassette import SimNet, SimStop, digest, llm_request, offline
from scripts.sim.fixtures import answer as answer_apify
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


def test_the_key_ignores_ids_and_times_but_not_the_question() -> None:
    def chat(text: str) -> dict:
        return llm_request("openrouter.ai", {**CHAT, "messages": [{"role": "user", "content": text}]})

    assert digest(chat("run 6f1d2c1e-8f3b-4a59-9d7e-1c2b3a4d5e6f at 2026-11-11T10:00:00Z")) == \
        digest(chat("run 0a1b2c3d-0000-4000-8000-000000000001 at 2026-11-12T23:59:59+01:00"))
    assert digest(chat("plot 2000 m2")) != digest(chat("plot 3000 m2"))


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


async def run(root: Path, *, responder=None, run_id: str = "land") -> tuple[int, dict]:
    from scripts.sim.harness import Bench, Persona

    persona = Persona.load(str(FIXTURES / "personas" / "land_madrid.json"))
    persona.env.update(SECRETS)
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
    assert [q["answer"] for q in first["questions"]][-2:] == ["до 3 млн евро", "[task:launch]"]
    assert {c["provider"] for c in first["campaign_costs"] if c["stage"] == "llm" and c["kind"] == "cost"} == \
        {"claude_cassette"} and first["costs_total_usd"] == 0
    assert first["pending"] == {"llm": [], "apify": [], "apify_blocked": []}

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
