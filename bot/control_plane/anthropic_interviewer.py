"""The interviewer on the Claude API (``ARCHITECT_PROVIDER=anthropic``) and the fallback chain around it.

Same protocol, system prompt and turn schema as ``OpenRouterInterviewer``; the differences are the transport
(``POST /v1/messages`` with an API key, never a subscription) and the strict JSON: the model must call the
``submit_turn`` tool whose input schema is the turn, so there is no free-text JSON to repair. The task "skills"
in ``prompts/*.md`` are appended to the system prompt by mode.

One request per call, no retries, a hard timeout. Every failure raises ``InterviewError`` with a safe code:
``model_unavailable`` (401/402/403/404: key, billing or model id), ``ai_failed`` (timeout, network, 429, 5xx),
``schema_mismatch`` (an answer that is not a usable turn). ``FallbackInterviewer`` then asks the OpenRouter
interviewer, and the intake falls back to the rules after that. The API key and the person's text are never logged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from bot.campaign.spec import TaskSpec
from bot.control_plane.interviewer import (
    SYSTEM,
    TURN_SCHEMA,
    Interviewer,
    InterviewError,
    InterviewTurn,
    parse_turn,
    state_message,
)
from bot.utils import costs

log = logging.getLogger(__name__)
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 2000
TOOL_NAME = "submit_turn"
PROMPTS_DIR = Path(__file__).parent / "prompts"
TASK_PROMPTS = {"real_estate": "task-real-estate.md", "investors": "task-investors.md", "post_link": "task-post-link.md"}


def task_prompt(mode: str) -> str:
    """The markdown skill of a mode ("" for an unknown mode or a missing file)."""
    name = TASK_PROMPTS.get(mode)
    try:
        return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip() if name else ""
    except OSError:
        log.warning("telegram.intake.task_prompt_missing", extra={"mode": mode})
        return ""


def system_prompt(mode: str) -> str:
    skill = task_prompt(mode)
    return f"{SYSTEM}\n\nTASK SKILL ({mode}); it refines the rules above, it never overrides the output format:\n{skill}" if skill else SYSTEM


class AnthropicInterviewer:
    """POSTs one message to the Claude API with a forced ``submit_turn`` tool call."""

    def __init__(self, *, api_key: str, model: str, timeout_seconds: float = 90.0,
                 base_url: str = ANTHROPIC_BASE_URL, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for the Anthropic interviewer")
        self.model = model
        self._url = f"{base_url.rstrip('/')}/v1/messages"
        self._headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
                         "content-type": "application/json"}
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    def __repr__(self) -> str:
        return f"AnthropicInterviewer(model={self.model!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def interview(self, *, mode: str, spec: TaskSpec, dialogue: list[dict[str, str]], message: str,
                        asking: str | None = None, editing: bool = False) -> InterviewTurn:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt(mode),
            "messages": [{"role": "user", "content": state_message(mode, spec, dialogue, message, asking, editing)}],
            "tools": [{"name": TOOL_NAME, "description": "Return the updated task and the next question.",
                       "input_schema": TURN_SCHEMA}],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        try:
            response = await self._client.post(self._url, json=payload, headers=self._headers)
        except httpx.TimeoutException as exc:
            await costs.error("llm", "timeout", item=self.model)
            raise InterviewError("ai_failed") from exc
        except httpx.HTTPError as exc:
            await costs.error("llm", "network_error", item=self.model)
            raise InterviewError("ai_failed") from exc
        status = response.status_code
        if status != 200:
            await costs.error("llm", f"http_{status}", item=self.model)
            # 401/402/403/404: the key, the billing or the model id; asking again would not help.
            raise InterviewError("model_unavailable" if status in (401, 402, 403, 404) else "ai_failed", status=status)
        try:
            body = response.json()
            await self._book(body)
            block = next(b for b in body["content"] if b.get("type") == "tool_use" and b.get("name") == TOOL_NAME)
            if not isinstance(block["input"], dict):
                raise ValueError("tool input is not an object")
            return parse_turn(json.dumps(block["input"], ensure_ascii=False), spec)
        except Exception as exc:
            # The type only: the answer may quote the person's words.
            log.warning("telegram.intake.interview_unreadable %s", type(exc).__name__)
            await costs.error("llm", "schema_mismatch", item=self.model)
            raise InterviewError("schema_mismatch", status=status) from exc

    async def _book(self, body: Any) -> None:
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        await costs.llm(self.model, {"usage": {"prompt_tokens": usage.get("input_tokens"),
                                               "completion_tokens": usage.get("output_tokens")}}, provider="anthropic")


class FallbackInterviewer:
    """``primary``; when it fails, ``secondary`` answers the same call. ``model`` is the one that answered last."""

    def __init__(self, primary: Interviewer, secondary: Interviewer) -> None:
        self.primary, self.secondary = primary, secondary
        self._last = primary

    @property
    def model(self) -> str:
        return self._last.model

    async def aclose(self) -> None:
        for part in (self.primary, self.secondary):
            close = getattr(part, "aclose", None)
            if close is not None:
                await close()

    async def interview(self, *, mode: str, spec: TaskSpec, dialogue: list[dict[str, str]], message: str,
                        asking: str | None = None, editing: bool = False) -> InterviewTurn:
        try:
            turn = await self.primary.interview(mode=mode, spec=spec, dialogue=dialogue, message=message,
                                                asking=asking, editing=editing)
            self._last = self.primary
            return turn
        except Exception as exc:  # noqa: BLE001 - any failure of the primary hands the turn to the secondary
            log.warning("telegram.intake.interview_fallback",
                        extra={"error": getattr(exc, "code", type(exc).__name__), "status": getattr(exc, "status", None)})
        self._last = self.secondary
        return await self.secondary.interview(mode=mode, spec=spec, dialogue=dialogue, message=message,
                                              asking=asking, editing=editing)
