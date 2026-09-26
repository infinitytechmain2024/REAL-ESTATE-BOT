from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .filters import filter_evidence
from .formatters import investors, real_estate
from .models import AnalysisResult, Evidence
from .openrouter import PROMPT_VERSION


@dataclass(frozen=True)
class PipelineOutcome:
    accepted: bool
    reason: str
    language: str
    result: AnalysisResult | None = None
    formatted: str | None = None


class AnalysisPipeline:
    def __init__(self, analyzer) -> None:
        self.analyzer = analyzer

    async def process(self, evidence: Evidence, vertical: str) -> PipelineOutcome:
        decision = filter_evidence(evidence, vertical)
        if not decision.accepted:
            return PipelineOutcome(False, decision.reason, decision.language)
        result = await self.analyzer.analyze(evidence, vertical)
        if not result.relevant or result.category not in (vertical,):
            return PipelineOutcome(False, "model_not_relevant", decision.language, result=result)
        formatter = real_estate if vertical == "real_estate" else investors
        return PipelineOutcome(
            True, "accepted", decision.language, result, formatter(result, evidence, decision.language)
        )


def finding_key(evidence: Evidence, vertical: str) -> str:
    return hashlib.sha256(f"{vertical}:{evidence.post_id}".encode()).hexdigest()


def metadata(language: str, model: str) -> dict[str, str]:
    return {"prompt_version": PROMPT_VERSION, "model": model, "language": language}
