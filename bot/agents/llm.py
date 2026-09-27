"""One JSON answer from a model on OpenRouter (Claude, Jev, Grok: all through ``OPENROUTER_API_KEY``).

A strict ``json_schema`` response format is asked for; a model that rejects it
(HTTP 400) is asked again in plain JSON mode with the same prompt. Errors carry
a short code that is safe to store and log (never the key, never the post).
"""

from __future__ import annotations

from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"


class LLMError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:80]


class OpenRouterJSON:
    def __init__(self, api_key: str, *, timeout_seconds: float = 30, client: httpx.AsyncClient | None = None,
                 url: str = OPENROUTER_URL) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required")
        self.url = url
        self._owns = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def unknown_models(self, models: list[str], *, url: str = MODELS_URL) -> list[str]:
        """Which of ``models`` OpenRouter's catalogue does not list. A ``~`` alias (e.g. ``~typesafe/jev-latest``)
        is resolved by OpenRouter itself and only its family is checked. Raises LLMError when the catalogue is
        unreachable."""
        try:
            response = await self._client.get(url, headers=self._headers)
            response.raise_for_status()
            listed = {str(m.get("id")) for m in response.json().get("data", []) if isinstance(m, dict)}
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise LLMError(f"catalogue:{type(exc).__name__}") from exc
        unknown = []
        for model in models:
            if model.startswith("~"):
                family = model[1:].rsplit("-latest", 1)[0]
                if not any(item.startswith(family) for item in listed):
                    unknown.append(model)
            elif model not in listed:
                unknown.append(model)
        return unknown

    async def complete(self, model: str, system: str, user: str, *, schema: dict[str, Any] | None = None,
                       name: str = "result", max_tokens: int = 1500) -> str:
        payload: dict[str, Any] = {
            "model": model, "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": ({"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
                                if schema else {"type": "json_object"}),
        }
        try:
            response = await self._client.post(self.url, headers=self._headers, json=payload)
            if response.status_code == 400 and schema:
                payload["response_format"] = {"type": "json_object"}
                response = await self._client.post(self.url, headers=self._headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError("timeout") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"network:{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise LLMError(f"http_{response.status_code}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("no_content") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("empty_content")
        return content
