"""Bounded OpenRouter speech-to-text adapter for Telegram voice notes.

One request per voice note: no retries, a hard timeout, and a size cap checked
before anything leaves the container. Transcription is billed per second, so a
retry loop would turn one flaky call into an open-ended bill.
"""

from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Protocol

import httpx

from bot.control_plane.models import TranscriptResult

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class Transcriber(Protocol):
    model: str
    provider: str

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult: ...


class TranscriptionError(RuntimeError):
    """A transcription attempt that failed; ``code`` is stored for the audit."""

    def __init__(self, code: str, detail: str, *, status: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.status = status


class OpenRouterTranscriber:
    """POSTs base64 audio to OpenRouter's ``/audio/transcriptions`` endpoint."""

    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_audio_bytes: int,
        base_url: str = OPENROUTER_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required for OpenRouter transcription")
        self.model = model
        self.max_audio_bytes = max_audio_bytes
        self._url = f"{base_url.rstrip('/')}/audio/transcriptions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
        if not audio:
            raise TranscriptionError("empty_audio", "voice payload is empty")
        if len(audio) > self.max_audio_bytes:
            raise TranscriptionError("too_large", f"{len(audio)} bytes exceeds {self.max_audio_bytes}")
        audio_format = (Path(filename).suffix.lstrip(".") or "ogg").lower()
        payload = {
            "model": self.model,
            "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": audio_format},
            # verbose_json carries the detected language when the provider has it.
            "response_format": "verbose_json",
        }
        try:
            response = await self._client.post(self._url, json=payload, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise TranscriptionError("timeout", "OpenRouter transcription timed out") from exc
        except httpx.HTTPError as exc:
            raise TranscriptionError("network_error", type(exc).__name__) from exc
        if response.status_code != 200:
            raise TranscriptionError("http_error", f"OpenRouter returned HTTP {response.status_code}", status=response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise TranscriptionError("bad_response", "response is not JSON", status=response.status_code) from exc
        if not isinstance(body, dict):
            raise TranscriptionError("bad_response", "response is not an object", status=response.status_code)
        text = str(body.get("text") or "").strip()
        if not text:
            raise TranscriptionError("empty_transcript", "no speech detected", status=response.status_code)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return TranscriptResult(
            text=text,
            language=_str_or_none(body.get("language")),
            confidence=_confidence(body.get("segments")),
            model=str(body.get("model") or self.model),
            provider=self.provider,
            cost_usd=_float_or_none(usage.get("cost")),
            audio_seconds=_float_or_none(usage.get("seconds", body.get("duration"))),
            request_status=response.status_code,
        )


def _str_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _confidence(segments: object) -> float | None:
    """Mean segment log-probability as a 0..1 score, when the provider sends segments."""
    if not isinstance(segments, list):
        return None
    log_probs = [p for s in segments if isinstance(s, dict) and (p := _float_or_none(s.get("avg_logprob"))) is not None]
    if not log_probs:
        return None
    return min(1.0, max(0.0, math.exp(sum(log_probs) / len(log_probs))))
