"""Golden tasks (PLAN 0.9): free text -> plan -> query task -> kind, portals, portal query, tolerance buckets.

``cases.json`` holds the 10 tasks. A case with ``spec_type`` is planned from a confirmed ``TaskSpec`` (the
interviewer's output, the only source of the property type for the tolerance rules); the others from the text alone.
A real bug is kept as a strict ``xfail`` in ``KNOWN_BUGS`` (never a weakened expectation).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bot.campaign import plan_campaign
from bot.campaign.spec import TaskSpec
from bot.campaign.tolerance import classify, min_area_of, request_for
from bot.web_search.queries import QueryTask, place_level_of, portal_query, task_kind

CASES: list[dict[str, Any]] = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))

# (case id, assertion) -> the bug it documents; strict xfail. Empty: all known bugs are fixed.
KNOWN_BUGS: dict[tuple[str, str], str] = {}


def _spec(case: dict[str, Any], plan_location: str) -> TaskSpec:
    data: dict[str, Any] = {"mode": "real_estate", "place": {"name": plan_location}, "deal": case["deal"],
                            "property_type": case["spec_type"], "budget": {"max": case["max_price"]}}
    if case["min_area"]:
        data["area_m2"] = {"min": case["min_area"]}
    return TaskSpec.model_validate(data)


def build(case: dict[str, Any]) -> tuple[Any, QueryTask]:
    plan = plan_campaign(case["text"])
    if case.get("spec_type"):
        plan = plan_campaign(case["text"], spec=_spec(case, plan.location))
    task = QueryTask(goal=plan.goal, task_text=case["text"], location=plan.location,
                     location_aliases=dict(plan.location_aliases), vertical=plan.vertical,
                     constraints=dict(plan.constraints), languages=tuple(plan.languages), country_code=plan.country,
                     place_level=place_level_of(case["text"], plan.location))
    return plan, task


def param(case: dict[str, Any], what: str, label: str | None = None) -> Any:
    reason = KNOWN_BUGS.get((case["id"], what))
    marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
    return pytest.param(case, id=label or case["id"], marks=marks)


def cases_for(what: str) -> list[Any]:
    return [param(c, what) for c in CASES]


def request_of(case: dict[str, Any], plan: Any) -> Any:
    return request_for(plan.constraints, location=plan.location, vertical=plan.vertical, text=case["text"],
                       country=plan.country)


def test_ten_cases() -> None:
    assert len(CASES) == 10 and len({c["id"] for c in CASES}) == 10
    assert all(len(c["listings"]) == 2 for c in CASES)


@pytest.mark.parametrize("case", cases_for("plan"))
def test_plan(case: dict[str, Any]) -> None:
    plan, _ = build(case)
    assert plan.vertical == case["vertical"]
    assert plan.constraints.get("deal") == case["deal"]
    assert plan.constraints.get("max_price") == case["max_price"]
    assert plan.constraints.get("rooms") == case["rooms"]
    assert request_of(case, plan).min_area == case["min_area"]


@pytest.mark.parametrize("case", cases_for("kind"))
def test_kind(case: dict[str, Any]) -> None:
    assert task_kind(build(case)[1]) == case["kind"]


@pytest.mark.parametrize("case", cases_for("portals"))
def test_portals(case: dict[str, Any]) -> None:
    assert list(build(case)[1].portals()[:4]) == case["portals"]


@pytest.mark.parametrize("case", [c for c in CASES if c["idealista"]], ids=lambda c: c["id"])
def test_idealista_query(case: dict[str, Any]) -> None:
    text = portal_query(build(case)[1], "idealista.com").text
    for part in case["idealista"]:
        assert part in text, (part, text)


@pytest.mark.parametrize("case", cases_for("place_level"))
def test_place_level(case: dict[str, Any]) -> None:
    assert build(case)[1].place_level == case["place_level"]


@pytest.mark.parametrize("case", [param(c, f"tolerance:{i}", f"{c['id']}-{i}") for c in CASES for i in (0, 1)])
def test_tolerance(case: dict[str, Any], request: pytest.FixtureRequest) -> None:
    index = int(request.node.callspec.id.rsplit("-", 1)[1])
    plan, _ = build(case)
    sample = case["listings"][index]
    assert classify(sample["listing"], request_of(case, plan), vertical=plan.vertical).bucket == sample["bucket"]


def test_spanish_minimum_area() -> None:
    assert min_area_of("Terreno en Madrid de al menos 2000 m²") == 2000
