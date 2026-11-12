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
from scripts.sim.cassette import SimNet, SimStop, digest, llm_request, mark_now, offline
from scripts.sim.fixtures import answer as answer_apify
from scripts.sim.harness import _questions
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


def test_a_portal_fixture_gives_only_the_rows_the_input_filters_let_through(tmp_path: Path) -> None:
    folder = tmp_path / "pending_apify"
    folder.mkdir()
    for name, actor, actor_input in (
            ("a", "igolaizola/idealista-scraper",  # sizes 67, 179, 232 m2: only 232 is from 200
             {"location": "Madrid", "operation": "sale", "propertyType": "lands", "minSize": "200"}),
            ("b", "igolaizola/idealista-scraper",
             {"location": "Madrid", "operation": "sale", "propertyType": "lands", "minSize": "300"}),
            ("c", "logiover/pisos-com-property-scraper",  # areas None, 1063, 232, 860; prices up to 2.1 M
             {"location": "madrid_capital", "transactionType": "sale", "propertyType": "land", "minArea": 800,
              "maxPrice": 2000000}),
            ("d", "igolaizola/idealista-scraper",  # no filter: the file as it is
             {"location": "Madrid", "operation": "sale", "propertyType": "lands"})):
        (folder / f"{name}.json").write_text(json.dumps({"actor": actor, "key": name, "input": actor_input}))
    answered, left = answer_apify(tmp_path)
    assert left == [] and "1 of 3 rows" in answered[0] and "0 of 3 rows" in answered[1]
    rows = {n: json.loads((tmp_path / "apify" / a / f"{n}.json").read_text(encoding="utf-8"))
            for n, a in (("a", "igolaizola~idealista-scraper"), ("b", "igolaizola~idealista-scraper"),
                         ("c", "logiover~pisos-com-property-scraper"))}
    assert [r["size"] for r in rows["a"]] == [232] and rows["b"] == []
    # a row without the area is kept (the portal's handling is unknown), 232 m2 and 2.1 M are not
    assert [(r["areaSqm"], r["price"]) for r in rows["c"]] == [(None, 1550000), (1063, 590000)]
    assert (tmp_path / "apify" / "igolaizola~idealista-scraper" / "d.json").read_bytes() == \
        (LIVE / "idealista_items.json").read_bytes()


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
                             "press": "near:yes:similar:00000000-0000-4000-8000-00000000000b"}]
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
    answers = [q["answer"] for q in first["questions"]]
    start = answers.index("до 3 млн евро")
    assert answers[start:start + 2] == ["до 3 млн евро", "[task:launch]"]
    # «Показать?» after the card (similar, then the farther ones), if the run held any: the fixture says «Нет»
    assert all(a.startswith("[near:no:") for a in answers[start + 2:]) and len(first["offers"]) <= 2
    assert [o["press"] for o in first["offers"]] == [a.strip("[]") for a in answers[start + 2:]]
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


@postgres
@pytest.mark.asyncio
async def test_the_card_is_launched_from_the_bottom_keyboard(tmp_path: Path) -> None:
    root = sim_root(tmp_path)
    (root / "answers" / "land.json").write_text(json.dumps(
        {"answers": [{"text": "до 3 млн евро"}, {"press": "▶️ Запустить"}, {"press": "Нет"}, {"press": "Нет"}]},
        ensure_ascii=False), encoding="utf-8")
    code, result = await run(root, responder=model.answer)
    assert code == 0 and result["status"] == "done", result["stop_reason"]
    shown = next(d for d in result["dialogue"] if d["from"] == "bot" and d["text"].startswith("Проверьте задачу:"))
    assert shown["keyboard"] == [["▶️ Запустить", "✏️ Изменить"], ["❌ Отмена"]]
    assert {"from": "user", "text": "▶️ Запустить", "press": "▶️ Запустить"} in result["dialogue"]
    assert result["campaign"]["state"] == "completed"
    [card] = result["cards"]
    assert "Площадь: 2 519 м² (участок)" in card["card"]  # CARD_EXTRA_FIELDS=true, as in prod
