"""The single interface every LLM provider implements.

Adding a provider means writing one class here-adjacent and decorating it with
:func:`bot.services.llm.registry.register_llm`. Nothing in the pipeline, the
handlers or the config schema needs to change -- the provider is selected by
name from ``LLM_PROVIDER``.
"""

from __future__ import annotations

import abc
import json
import re
from collections.abc import Callable
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.exceptions import LLMResponseError

Role = Literal["system", "user", "assistant"]

ModelT = TypeVar("ModelT", bound=BaseModel)


class ChatMessage(BaseModel):
    """One turn of the conversation sent to a provider."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> ChatMessage:
        return cls(role="assistant", content=content)


class Usage(BaseModel):
    """Token accounting, when the provider reports it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMResponse(BaseModel):
    """A provider's reply, normalised across SDKs."""

    text: str
    model: str
    provider: str
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


UsageSink = Callable[[LLMResponse], None]
"""Notified of every provider response so its tokens can be accounted for."""


class LLMProvider(abc.ABC):
    """Base class for chat-completion providers.

    Subclasses implement :meth:`chat`; :meth:`chat_structured` is provided here
    so every provider gets schema-validated output with the same repair
    behaviour, whether or not the backend supports native JSON mode.
    """

    name: ClassVar[str]
    """Registry key, e.g. ``"anthropic"``. Set by the subclass."""

    supports_json_mode: ClassVar[bool] = False
    """Whether :meth:`chat` honours ``json_schema`` natively."""

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_schema: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        """Send *messages* and return the assistant's reply.

        Implementations must raise :class:`bot.exceptions.LLMError` (or a
        subclass) for every failure, so the manager can fall back cleanly.
        """

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Release the underlying HTTP client."""

    async def chat_structured(
        self,
        messages: list[ChatMessage],
        schema: type[ModelT],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float | None = None,
        repair_attempts: int = 1,
        usage_sink: UsageSink | None = None,
    ) -> ModelT:
        """Return the reply parsed into *schema*.

        Providers with native JSON mode get the JSON schema passed through;
        the rest are steered with an instruction appended to the system
        message. Either way the text is parsed defensively (models like to
        wrap JSON in prose or ```json fences) and, if that still fails, the
        model is shown its own broken output and asked to fix it.

        *usage_sink* is called with every underlying :class:`LLMResponse`,
        repair attempts included. Without it the token counts for this path
        would be dropped on the floor -- and a repair round costs real money,
        so it is exactly the call that must not go unmetered.
        """
        json_schema = schema.model_json_schema()
        prepared = list(messages)
        if not self.supports_json_mode:
            prepared = _append_schema_instruction(prepared, json_schema)

        response = await self.chat(
            prepared,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_schema=json_schema if self.supports_json_mode else None,
            timeout=timeout,
        )
        if usage_sink is not None:
            usage_sink(response)

        last_error: Exception
        text = response.text
        for attempt in range(repair_attempts + 1):
            try:
                return schema.model_validate(extract_json(text))
            except (LLMResponseError, ValidationError) as exc:
                last_error = exc
                if attempt >= repair_attempts:
                    break
                repair = [
                    *prepared,
                    ChatMessage.assistant(text[:4000]),
                    ChatMessage.user(
                        "That was not valid for the requested schema. Error:\n"
                        f"{exc}\n\nReply again with JSON only -- no prose, no code fences."
                    ),
                ]
                response = await self.chat(
                    repair,
                    model=model,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    json_schema=json_schema if self.supports_json_mode else None,
                    timeout=timeout,
                )
                if usage_sink is not None:
                    usage_sink(response)
                text = response.text

        raise LLMResponseError(
            f"{self.name} did not return valid {schema.__name__}: {last_error}"
        )


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model reply.

    Handles the three things models actually do: return clean JSON, wrap it in
    a ``` fence, or bury it in a sentence. Raises
    :class:`LLMResponseError` when there is no JSON to be found.
    """
    candidate = (text or "").strip()
    if not candidate:
        raise LLMResponseError("empty response from model")

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    fenced = _FENCE.search(candidate)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            candidate = fenced.group(1)

    # Fall back to the outermost {...} or [...] span.
    for opening, closing in (("{", "}"), ("[", "]")):
        start = candidate.find(opening)
        end = candidate.rfind(closing)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise LLMResponseError(f"no JSON object found in response: {candidate[:200]!r}")


def _append_schema_instruction(
    messages: list[ChatMessage], json_schema: dict[str, Any]
) -> list[ChatMessage]:
    """Bolt a 'reply with this JSON schema' instruction onto the system turn."""
    instruction = (
        "Reply with a single JSON value and nothing else -- no prose, no markdown "
        "fences. It must validate against this JSON Schema:\n"
        f"{json.dumps(json_schema, ensure_ascii=False)}"
    )
    if messages and messages[0].role == "system":
        head = ChatMessage.system(f"{messages[0].content}\n\n{instruction}")
        return [head, *messages[1:]]
    return [ChatMessage.system(instruction), *messages]
