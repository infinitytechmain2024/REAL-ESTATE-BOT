"""The outside world of a simulated run: LLM and Apify answers from files, Telegram into a list, nothing else.

``SimNet.handle`` answers every ``httpx`` request of the process (``offline`` patches ``httpx.AsyncHTTPTransport``,
the transport every client of the bot ends up on, so no production constructor changes):

* ``openrouter.ai`` / ``api.anthropic.com`` chat calls: key = hash(model, messages, response schema or tools) ->
  ``sim/cassette/<key>.json``; a miss writes ``sim/pending/<key>.json`` (stage, system, user, schema);
* ``api.apify.com``: ``POST acts/<actor>/runs`` with an input -> ``sim/apify/<actor>/<hash of input>.json`` (or
  ``_default.json`` in that folder); the run's life is emulated (RUNNING -> SUCCEEDED, dataset pages); a miss writes
  ``sim/pending_apify/<actor>__<hash>.json``;
* ``api.telegram.org``: every sendMessage / editMessageText / deleteMessage is kept (what the user saw);
* any other host (SearXNG, sites): ``sim/http/<host>/<hash>.json`` if recorded, else an empty search result or a 503,
  listed in ``offline``.

Headers are never read or written: no key or token reaches a file. Hashes are taken after ``normalize``: UUIDs and
the bench's «now» (a date or a timestamp of today, of the virtual clock or of the run's start, ``mark_now``) are
blanked, so a replay on another day hits the same answers; any other date (a listing's) stays in the key.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import re
import socket
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

LLM_HOSTS = frozenset({"openrouter.ai", "api.anthropic.com"})
APIFY_HOST = "api.apify.com"
TELEGRAM_HOST = "api.telegram.org"
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_DATETIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_DATE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
# The modules that only carry a call (the stage is their caller).
_CARRIERS = ("bot.agents.llm", "bot.acquisition.apify_client", "bot.acquisition.apify_source")
STAGES = {"interviewer": "interview", "anthropic_interviewer": "interview", "openrouter": "analysis",
          "queries": "queries"}


class SimStop(BaseException):
    """A strict run (``--step``) stops at the first missing answer. A BaseException: no ``except Exception`` of the bot
    turns it into a fallback."""

    def __init__(self, kind: str, path: Path) -> None:
        super().__init__(f"{kind}: {path}")
        self.kind, self.path = kind, path


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_NOW_DATES: set[str] = set()


def mark_now(*moments: datetime) -> None:
    """Dates that are «now» for this process (the virtual clock, the run's start): blanked in a key."""
    _NOW_DATES.update(m.astimezone(UTC).date().isoformat() for m in moments)
    _NOW_DATES.update(m.date().isoformat() for m in moments)


def now_dates() -> set[str]:
    return {datetime.now(UTC).date().isoformat(), date.today().isoformat(), *_NOW_DATES}


def normalize(text: str) -> str:
    """UUIDs and «now» out of a hashed text: they change from run to run, the question does not. A timestamp or a
    date is «now» when its day is today or a day of the bench's clock; a listing's date is data and stays."""
    dates = now_dates()
    text = _UUID.sub("<uuid>", text)
    text = _DATETIME.sub(lambda m: "<now>" if m.group(0)[:10] in dates else m.group(0), text)
    return _DATE.sub(lambda m: "<today>" if m.group(0) in dates else m.group(0), text)


def digest(value: Any, size: int = 24) -> str:
    return hashlib.sha256(normalize(canonical(value)).encode()).hexdigest()[:size]


def llm_request(host: str, body: dict[str, Any]) -> dict[str, Any]:
    """The part of a chat request that decides the answer: provider, model, messages, schema or tools."""
    if host == "api.anthropic.com":
        messages = [{"role": "system", "content": body.get("system", "")}, *body.get("messages", [])]
        return {"provider": "anthropic", "model": body.get("model", ""), "messages": messages,
                "schema": body.get("tools")}
    response_format = body.get("response_format") or {}
    schema: Any = None
    if response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
    elif response_format:
        schema = response_format.get("type")
    if body.get("tools"):
        schema = body["tools"]
    return {"provider": "openrouter", "model": body.get("model", ""), "messages": body.get("messages", []),
            "schema": schema}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def caller() -> str:
    """``module:function`` of the bot code that made the call (the first ``bot.`` frame that is not a carrier)."""
    frame = sys._getframe(1)
    while frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
        if module.startswith("bot.") and not module.startswith(_CARRIERS):
            return f"{module}:{frame.f_code.co_qualname}"
        frame = frame.f_back
    return "unknown"


def stage_of(where: str) -> str:
    module = where.split(":", 1)[0]
    tail = module.rsplit(".", 1)[-1]
    if tail == "queries":
        return "social_queries" if module.startswith("bot.social_search") else "web_queries"
    return STAGES.get(tail, tail)


@dataclass
class Telegram:
    """Every Bot API call of the run, in order; edits change the text of a kept message."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1000))

    def call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        if method == "sendMessage":
            message_id = next(self._ids)
            markup = body.get("reply_markup") or {}
            buttons = [{"text": b.get("text"), "data": b.get("callback_data")}
                       for row in markup.get("inline_keyboard", []) for b in row]
            self.messages.append({"message_id": message_id, "chat_id": body.get("chat_id"), "text": body.get("text", ""),
                                  "buttons": buttons, "edits": 0, "deleted": False, "source": "runner"})
            return {"ok": True, "result": {"message_id": message_id, "chat": {"id": body.get("chat_id")}}}
        if method in ("editMessageText", "deleteMessage"):
            for message in self.messages:
                if message["message_id"] == body.get("message_id") and message["chat_id"] == body.get("chat_id"):
                    if method == "deleteMessage":
                        message["deleted"] = True
                    elif message["text"] == body.get("text"):
                        return {"ok": False, "description": "Bad Request: message is not modified"}
                    else:
                        message["text"], message["edits"] = body.get("text", ""), message["edits"] + 1
                    return {"ok": True, "result": True}
            return {"ok": False, "description": "Bad Request: message to edit not found"}
        return {"ok": True, "result": True}


@dataclass
class _Run:
    id: str
    actor: str
    dataset: str
    items: list[Any]
    usage: float
    status: str = "RUNNING"
    max_items: int | None = None  # ``maxItems`` of the launch: the dataset never holds more


class SimNet:
    """Answers every request of a simulated run. ``strict``: the first missing answer raises ``SimStop``;
    otherwise the miss is written to pending, the bot gets a refusal and goes on (more pending in one pass)."""

    def __init__(self, root: Path, *, strict: bool = False, run_id: str = "",
                 responder: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None) -> None:
        """``responder``: answers a missing LLM call in-process (tests: a synthetic model); its answer is written to
        the cassette as an agent's would be. None (the CLI): a miss is pending."""
        self.root, self.strict, self.run_id, self.responder = root, strict, run_id, responder
        self.telegram = Telegram()
        self.pending_llm: list[str] = []
        self.pending_apify: list[str] = []
        self.blocked_apify: list[str] = []  # misses after an unanswered LLM call: their input may be a fallback's
        self.hits: dict[str, int] = {"llm": 0, "apify": 0, "http": 0}
        self.llm_calls: list[dict[str, Any]] = []
        self.offline: list[dict[str, Any]] = []
        self.unhandled: list[str] = []
        self.calls = 0  # every answered request: the driver's sign of progress
        self._runs: dict[str, _Run] = {}
        self._datasets: dict[str, _Run] = {}
        self._run_ids = itertools.count(1)
        self._asked: set[str] = set()  # the messages already pending in this pass (a retry with another model)

    # -- dispatch --

    async def handle(self, request: httpx.Request) -> httpx.Response:
        host = (request.url.host or "").lower()
        body = await request.aread()
        if host in LLM_HOSTS:
            return self._llm(host, request, body)
        if host == APIFY_HOST:
            return self._apify(request, body)
        if host == TELEGRAM_HOST:
            method = request.url.path.rsplit("/", 1)[-1]
            data = _json(body) or {}
            self.calls += 1
            return httpx.Response(200, json=self.telegram.call(method, data))
        return self._other(host, request, body)

    # -- LLM --

    def _llm(self, host: str, request: httpx.Request, raw: bytes) -> httpx.Response:
        body = _json(raw)
        if not isinstance(body, dict) or not request.url.path.endswith(("/chat/completions", "/v1/messages")):
            if request.url.path.endswith("/models"):  # the reduction worker's catalogue check
                self.calls += 1
                return httpx.Response(200, json={"data": []})
            self.unhandled.append(f"{request.method} {host}{request.url.path}")
            return httpx.Response(404, json={"error": {"message": "sim: unknown endpoint"}})
        asked = llm_request(host, body)
        key = digest(asked)
        where = caller()
        self.llm_calls.append({"key": key, "stage": stage_of(where), "model": asked["model"]})
        path = self.root / "cassette" / f"{key}.json"
        question = self._question(asked, key, where)
        if not path.is_file() and self.responder is not None and (answer := self.responder(question)) is not None:
            _write(path, answer)
        if path.is_file():
            entry = json.loads(path.read_text(encoding="utf-8"))
            self.hits["llm"] += 1
            self.calls += 1
            if entry.get("error_status"):
                return httpx.Response(int(entry["error_status"]), json={"error": {"message": "sim: recorded error"}})
            return httpx.Response(200, json=_completion(host, body, entry, key))
        pending = self.root / "pending" / f"{key}.json"
        same = digest(asked["messages"])
        if same not in self._asked:  # the same messages again (a fallback model): one pending is enough
            self._asked.add(same)
            _write(pending, question)
            self.pending_llm.append(key)
        if self.strict:
            raise SimStop("llm", pending)
        return httpx.Response(503, json={"error": {"message": "sim: waiting for an answer"}})

    def _question(self, asked: dict[str, Any], key: str, where: str) -> dict[str, Any]:
        """The pending file of a call: what the model of the stage gets, nothing of the request's headers."""
        messages = asked["messages"]
        return {
            "kind": "llm", "key": key, "run": self.run_id, "stage": stage_of(where), "caller": where,
            "provider": asked["provider"], "model": asked["model"],
            "speculative": bool(self.pending_llm or self.pending_apify),
            "system": "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system"),
            "user": "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") != "system"),
            "schema": asked["schema"],
            "answer_to": f"sim/cassette/{key}.json",
            "answer_format": ('{"content": "<the model\'s JSON answer as a string>"} or '
                              '{"tool_call": {"name": "...", "input": {...}}}'),
        }

    # -- Apify --

    def _apify(self, request: httpx.Request, raw: bytes) -> httpx.Response:
        path = request.url.path.removeprefix("/v2/").strip("/")
        parts = path.split("/")
        method = request.method
        if method == "POST" and len(parts) == 3 and parts[0] == "acts" and parts[2] == "runs":
            return self._launch(parts[1], request, raw)
        if parts[0] == "actor-runs" and len(parts) >= 2:
            run = self._runs.get(parts[1])
            if run is None:
                return httpx.Response(404, json={"error": {"type": "record-not-found"}})
            if method == "POST" and parts[2:] == ["abort"]:
                run.status = "ABORTED" if run.status not in ("SUCCEEDED", "FAILED") else run.status
            elif method == "GET" and len(parts) == 2 and run.status == "RUNNING":
                run.status = "SUCCEEDED"  # the first look after the launch finds it done
            elif method == "GET" and parts[2:] == ["dataset", "items"]:
                return self._items(run, request)
            self.calls += 1
            return httpx.Response(200, json={"data": _run_data(run)})
        if method == "GET" and len(parts) == 3 and parts[0] == "datasets" and parts[2] == "items":
            run = self._datasets.get(parts[1])
            if run is None:
                return httpx.Response(404, json={"error": {"type": "record-not-found"}})
            return self._items(run, request)
        if method == "GET" and len(parts) == 2 and parts[0] == "acts":
            self.calls += 1
            return httpx.Response(200, json={"data": {"id": parts[1], "name": parts[1].split("~")[-1]}})
        self.unhandled.append(f"{method} {APIFY_HOST}/{path}")
        return httpx.Response(404, json={"error": {"type": "sim-unknown-endpoint"}})

    def _launch(self, actor: str, request: httpx.Request, raw: bytes) -> httpx.Response:
        actor_input = _json(raw)
        key = digest(actor_input, 16)
        folder = self.root / "apify" / actor
        record = next((p for p in (folder / f"{key}.json", folder / "_default.json") if p.is_file()), None)
        if record is None:
            pending = self.root / "pending_apify" / f"{actor}__{key}.json"
            if self.pending_llm:  # the input may come from a fallback of an unanswered call: never pay for it
                self.blocked_apify.append(f"{actor}__{key}")
            else:
                _write(pending, {"kind": "apify", "run": self.run_id, "actor": actor.replace("~", "/"),
                                 "key": key, "stage": stage_of(caller()),
                                 "params": dict(request.url.params), "input": actor_input,
                                 "answer_to": f"sim/apify/{actor}/{key}.json",
                                 "answer_format": '[<dataset items>] or {"items": [...], "usage_usd": 0.0}'})
                self.pending_apify.append(f"{actor}__{key}")
            if self.strict:
                raise SimStop("apify", pending)
            # A 4xx says nothing was started: the source fails for this campaign, the rest goes on.
            return httpx.Response(400, json={"error": {"type": "sim-pending", "message": "no recorded run"}})
        data = json.loads(record.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("items", [])
        usage = 0.0 if isinstance(data, list) else float(data.get("usage_usd", 0.0))
        n = next(self._run_ids)
        limit = _int(request.url.params.get("maxItems"))
        run = _Run(f"simrun{n}", actor, f"simds{n}", list(items)[:limit] if limit is not None else list(items), usage,
                   "RUNNING" if isinstance(data, list) else str(data.get("status", "RUNNING")), limit)
        self._runs[run.id], self._datasets[run.dataset] = run, run
        self.hits["apify"] += 1
        self.calls += 1
        return httpx.Response(201, json={"data": _run_data(run)})

    def _items(self, run: _Run, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        offset = int(params.get("offset", 0) or 0)
        limit = int(params.get("limit", len(run.items)) or len(run.items))
        self.calls += 1
        return httpx.Response(200, json=run.items[offset:offset + limit])

    # -- everything else --

    def _other(self, host: str, request: httpx.Request, raw: bytes) -> httpx.Response:
        key = digest({"method": request.method, "url": str(request.url.copy_with(query=None)),
                      "query": sorted(request.url.params.multi_items()), "body": raw.decode("utf-8", "replace")}, 16)
        path = self.root / "http" / host / f"{key}.json"
        if path.is_file():
            entry = json.loads(path.read_text(encoding="utf-8"))
            self.hits["http"] += 1
            self.calls += 1
            if "json" in entry:
                return httpx.Response(int(entry.get("status", 200)), json=entry["json"])
            return httpx.Response(int(entry.get("status", 200)), text=str(entry.get("text", "")),
                                  headers={"content-type": entry.get("content_type", "text/html; charset=utf-8")})
        self.offline.append({"host": host, "method": request.method, "path": request.url.path, "key": key})
        if request.url.path.rstrip("/").endswith("/search"):  # SearXNG: nothing found
            return httpx.Response(200, json={"results": [], "number_of_results": 0})
        return httpx.Response(503, text="sim: offline")


def _run_data(run: _Run) -> dict[str, Any]:
    done = run.status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")
    return {"id": run.id, "actId": run.actor, "status": run.status, "defaultDatasetId": run.dataset,
            "usageTotalUsd": run.usage if done else None, "stats": {}}


def _completion(host: str, body: dict[str, Any], entry: dict[str, Any], key: str) -> dict[str, Any]:
    call = entry.get("tool_call")
    content = entry.get("content")
    if host == "api.anthropic.com":
        tools = body.get("tools") or [{}]
        if call is not None:
            name, value = call.get("name") or tools[0].get("name"), call.get("input", call.get("arguments"))
        else:
            name, value = tools[0].get("name"), json.loads(content) if isinstance(content, str) else content
        if isinstance(value, str):
            value = json.loads(value)
        return {"id": f"msg_sim_{key}", "type": "message", "role": "assistant", "model": body.get("model"),
                "content": [{"type": "tool_use", "id": f"toolu_sim_{key}", "name": name, "input": value}],
                "stop_reason": "tool_use", "usage": {"input_tokens": 0, "output_tokens": 0}}
    if call is not None:
        content = call.get("input", call.get("arguments"))
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return {"id": f"sim-{key}", "object": "chat.completion", "model": body.get("model"), "provider": "claude_cassette",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0}}


def _int(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class _NoCurl:
    """``bot.web_search.fetcher.CurlTransport`` for the run: curl_cffi (libcurl, past httpx and the socket guard) is
    never used; an impersonating fetcher reads through httpx, which ``net`` answers."""

    def __new__(cls, *, profile: str, timeout_seconds: float = 20) -> Any:
        from bot.web_search.fetcher import HttpxTransport

        transport = HttpxTransport(timeout_seconds=timeout_seconds)
        transport.profile = profile  # type: ignore[attr-defined]
        return transport


@contextlib.contextmanager
def offline(net: SimNet) -> Iterator[SimNet]:
    """Every ``httpx`` request of the process goes to ``net``; a socket may connect to the loopback only; the web
    fetcher's curl_cffi transport is replaced by httpx (``_NoCurl``)."""
    from bot.web_search import fetcher

    curl = fetcher.CurlTransport
    async_send = httpx.AsyncHTTPTransport.handle_async_request
    sync_send = httpx.HTTPTransport.handle_request
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    async def routed(_self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return await net.handle(request)

    def refused(_self: Any, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sim: synchronous network is off", request=request)

    def guard(address: Any) -> None:
        if isinstance(address, tuple) and address and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise OSError(f"sim: no network ({address[0]})")

    def checked_connect(self: socket.socket, address: Any) -> None:
        guard(address)
        connect(self, address)

    def checked_connect_ex(self: socket.socket, address: Any) -> int:
        guard(address)
        return connect_ex(self, address)

    httpx.AsyncHTTPTransport.handle_async_request = routed  # type: ignore[method-assign]
    httpx.HTTPTransport.handle_request = refused  # type: ignore[method-assign]
    socket.socket.connect = checked_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = checked_connect_ex  # type: ignore[method-assign]
    fetcher.CurlTransport = _NoCurl  # type: ignore[assignment,misc]
    try:
        yield net
    finally:
        fetcher.CurlTransport = curl  # type: ignore[misc]
        httpx.AsyncHTTPTransport.handle_async_request = async_send  # type: ignore[method-assign]
        httpx.HTTPTransport.handle_request = sync_send  # type: ignore[method-assign]
        socket.socket.connect, socket.socket.connect_ex = connect, connect_ex  # type: ignore[method-assign]
