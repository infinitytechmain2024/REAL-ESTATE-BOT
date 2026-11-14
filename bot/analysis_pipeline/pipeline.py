from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .filters import filter_evidence
from .formatters import investors, real_estate
from .models import AnalysisResult, Evidence
from .openrouter import prompt_version
from .prefilter import news_page

INVESTOR_NEWS = "investor_news_page"  # INVESTOR_LEAD_SOURCE_CHECK (Q53): booked as the skip «prefilter_investor_news_page»


@dataclass(frozen=True)
class PipelineOutcome:
    accepted: bool
    reason: str
    language: str
    result: AnalysisResult | None = None
    formatted: str | None = None
    location_from_text: bool = False  # ANALYSIS_LOCATION_FROM_TEXT of the analyzer: its own prompt version
    agency_rule: bool = False  # PORTAL_DESCRIPTION_FULL (Q18) of the analyzer: «-agency» on the «-loc» version
    investor_rule: bool = False  # INVESTOR_LEAD_SOURCE_CHECK (Q53), an investor post: «-isrc» on the version


class AnalysisPipeline:
    def __init__(self, analyzer) -> None:
        self.analyzer = analyzer

    async def process(
        self, evidence: Evidence, vertical: str, task_hint: dict[str, Any] | None = None
    ) -> PipelineOutcome:
        decision = filter_evidence(evidence, vertical)
        if not decision.accepted:
            return PipelineOutcome(False, decision.reason, decision.language)
        investor_rule = vertical == "investors" and getattr(self.analyzer, "investor_lead_check", False) is True
        if investor_rule and news_page(evidence.canonical_url):
            return PipelineOutcome(False, INVESTOR_NEWS, decision.language)  # news is not a lead: no model call
        # The hint is passed only when the post belongs to a campaign (analyzers without the parameter keep working).
        result = await (
            self.analyzer.analyze(evidence, vertical, task_hint=task_hint)
            if task_hint
            else self.analyzer.analyze(evidence, vertical)
        )
        if not result.relevant or result.category not in (vertical,):
            return PipelineOutcome(False, "model_not_relevant", decision.language, result=result)
        formatter = real_estate if vertical == "real_estate" else investors
        return PipelineOutcome(
            True, "accepted", decision.language, result, formatter(result, evidence, decision.language),
            location_from_text=getattr(self.analyzer, "location_from_text", False) is True,
            agency_rule=getattr(self.analyzer, "agency_rule", False) is True, investor_rule=investor_rule,
        )


def finding_key(evidence: Evidence, vertical: str) -> str:
    return hashlib.sha256(f"{vertical}:{evidence.post_id}".encode()).hexdigest()


def metadata(language: str, model: str, *, location_from_text: bool = False,
             agency_rule: bool = False, investor_rule: bool = False) -> dict[str, str]:
    """``analysis_metadata`` of a finding; ``location_from_text`` (ANALYSIS_LOCATION_FROM_TEXT), ``agency_rule``
    (PORTAL_DESCRIPTION_FULL) and ``investor_rule`` (INVESTOR_LEAD_SOURCE_CHECK) mark its prompt."""
    return {"prompt_version": prompt_version(location_from_text, agency_rule, investor_rule), "model": model,
            "language": language}
