"""Thirty task/status/scope cases for the factual claims in the user report.

These are release checks for the report's wording, not a substitute for the
two-person usefulness rating required by FINAL_ANSWER_QUALITY_PLAN.md.
"""

from __future__ import annotations

import pytest

from bot.campaign.final_report import (
    FinalReporter,
    HeldEvidence,
    PlaceScope,
    task_title,
)
from bot.campaign.runs import OutcomeCount, StreamFinding
from bot.campaign.tolerance import Request


CRITERIA = (
    ("area", "площадь", "площадь от 2000 м²"),
    ("budget", "цена", "цена"),
    ("must_have:под застройку", "под застройку", "под застройку"),
    ("must_have:метро 5 минут на машине", "метро 5 минут на машине", "метро 5 минут на машине"),
    ("must_have:лифт", "лифт", "лифт"),
)


@pytest.mark.parametrize("criterion,unknown_label,pass_label", CRITERIA)
@pytest.mark.parametrize("verdict", ("pass", "fail", "unknown"))
@pytest.mark.parametrize("suburb", (False, True))
async def test_report_uses_the_actual_criterion_verdict_and_geographic_scope(
    criterion: str, unknown_label: str, pass_label: str, verdict: str, suburb: bool,
) -> None:
    """A claim is tied to the saved matrix; city and suburb requests stay distinct."""
    is_flat = criterion == "must_have:лифт"
    request = Request(location="Madrid", min_area=None if is_flat else 2000,
                      amount=200000 if criterion == "budget" else None,
                      property_type="apartment" if is_flat else "land")
    scope = PlaceScope("Madrid", suburb, 25 if suburb else None)
    requested_detail = {
        "area": "от 2000 м²", "budget": "до 200000 €", "must_have:под застройку": "под застройку",
        "must_have:метро 5 минут на машине": "в 5 минутах от метро на машине", "must_have:лифт": "с лифтом",
    }[criterion]
    task = (f"Купить {'квартиру' if is_flat else 'участок'} {requested_detail} "
            f"{'в пригороде' if suburb else 'в'} Мадрида")
    finding = StreamFinding("held-1", "Участок", payload={"address": "Aravaca, Madrid, Madrid"},
                            url="https://example.com/private-card")
    held = [HeldEvidence(finding, {"prompt": "reviewer-v3", "criteria": [
        {"name": criterion, "verdict": verdict}]})]
    text = await FinalReporter().build(
        task_title(request, scope=scope), request, [OutcomeCount("held", "similar", "unverified", 1)],
        [], [], [], task_text=task, held=held, scope=scope,
    )

    assert "Подтверждённых совпадений нет" in text
    assert "https://example.com/private-card" not in text
    if suburb:
        assert "радиус 25 км" in text
        assert "пригородность по такому адресу не доказана" in text
    else:
        assert "радиус 25 км" not in text
        assert "пригородность по такому адресу не доказана" not in text
    if verdict == "pass":
        assert f"проходит условие: {pass_label}" in text
    elif verdict == "fail":
        assert f"явно не выполнено: {unknown_label}" in text
    else:
        assert f"не подтверждено: {unknown_label}" in text
    assert len(text) <= 3900
