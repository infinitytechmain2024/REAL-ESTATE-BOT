"""Reduction step 3: the decision gate (docs/HYBRID_AGENTS.md, Task 2.4). A pure function.

Order, first match wins:
1. deterministic guards (``tolerance.classify``: not an offer, wrong deal, wrong
   country/currency, area under 75 %) -> discard; never overridden by a model;
2. spam above ``spam_max`` or credibility under ``credible_min`` -> discard;
3. any of relevant / offer / fit answered with low confidence (or missing) -> hold;
4. relevant and offer high enough: fit high enough AND the rules say exact -> send
   (bucket exact); otherwise hold with the rules' bucket (similar/other);
5. relevant at least ``hold_relevant_min`` -> hold (other);
6. otherwise -> discard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from bot.campaign.tolerance import Request, classify

from .jev import Answer

Action = Literal["send", "hold", "discard"]


@dataclass(frozen=True, slots=True)
class Policy:
    version: int = 1
    relevant_min: float = 0.70
    offer_min: float = 0.65
    fit_exact_min: float = 0.60
    credible_min: float = 0.55
    spam_max: float = 0.35
    min_confidence: float = 0.50
    hold_relevant_min: float = 0.50

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    bucket: str | None
    reason: str
    score: float | None
    rules_bucket: str


DEFAULT_POLICY = Policy()


def gate(extraction: dict[str, Any], answers: dict[str, Answer], request: Request, *, vertical: str | None,
         policy: Policy = DEFAULT_POLICY) -> Decision:
    rules = classify(extraction, request, vertical=vertical)
    score = _score(answers)
    if rules.bucket == "excluded":
        return Decision("discard", "excluded", f"rules:{rules.why or 'excluded'}", score, rules.bucket)
    spam, credible = answers.get("q_spam"), answers.get("q_credible")
    if spam is not None and spam.p > policy.spam_max:
        return Decision("discard", None, "jev:spam", score, rules.bucket)
    if credible is not None and credible.p < policy.credible_min:
        return Decision("discard", None, "jev:not_credible", score, rules.bucket)
    core = [answers.get(q) for q in ("q_relevant", "q_offer", "q_fit")]
    if any(a is None or a.confidence < policy.min_confidence for a in core):
        return Decision("hold", rules.bucket if rules.bucket != "exact" else "similar", "jev:low_confidence",
                        score, rules.bucket)
    relevant, offer, fit = core
    if relevant.p >= policy.relevant_min and offer.p >= policy.offer_min:
        if fit.p >= policy.fit_exact_min and rules.bucket == "exact":
            return Decision("send", "exact", "gate:send", score, rules.bucket)
        bucket = rules.bucket if rules.bucket != "exact" else "similar"
        return Decision("hold", bucket, "gate:near", score, rules.bucket)
    if relevant.p >= policy.hold_relevant_min:
        return Decision("hold", "other", "gate:weak", score, rules.bucket)
    return Decision("discard", None, "jev:irrelevant", score, rules.bucket)


def _score(answers: dict[str, Answer]) -> float | None:
    """0.4·relevant + 0.25·fit + 0.2·actionable + 0.15·credible (the ranking inside the final set)."""
    weights = {"q_relevant": 0.4, "q_fit": 0.25, "q_actionable": 0.2, "q_credible": 0.15}
    if not all(q in answers for q in weights):
        return None
    return round(sum(answers[q].p * w for q, w in weights.items()), 3)
