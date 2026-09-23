"""Local, bounded faster-whisper transcription adapter."""

from __future__ import annotations

import asyncio
import math
import tempfile
from pathlib import Path
from typing import Protocol

from bot.control_plane.models import TranscriptResult


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult: ...


class FasterWhisperTranscriber:
    """Lazy-loads the model once and keeps blocking inference off the event loop."""

    def __init__(self, *, model: str, device: str = "cpu", compute_type: str = "int8") -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self._model: object | None = None
        self._load_lock = asyncio.Lock()

    async def _get_model(self) -> object:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)
        return self._model

    def _load(self) -> object:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - covered by container build
            raise RuntimeError("faster-whisper is not installed") from exc
        return WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
        if not audio:
            raise ValueError("voice payload is empty")
        suffix = Path(filename).suffix or ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
            handle.write(audio)
            handle.flush()
            model = await self._get_model()
            return await asyncio.to_thread(self._transcribe_file, model, handle.name)

    def _transcribe_file(self, model: object, path: str) -> TranscriptResult:
        segments, info = model.transcribe(path, vad_filter=True, beam_size=5)  # type: ignore[attr-defined]
        collected = list(segments)
        text = " ".join(segment.text.strip() for segment in collected).strip()
        if not text:
            raise ValueError("no speech detected")
        log_probs = [segment.avg_logprob for segment in collected if getattr(segment, "avg_logprob", None) is not None]
        confidence = min(1.0, max(0.0, math.exp(sum(log_probs) / len(log_probs)))) if log_probs else None
        return TranscriptResult(text=text, language=getattr(info, "language", None), confidence=confidence, model=self.model_name)
