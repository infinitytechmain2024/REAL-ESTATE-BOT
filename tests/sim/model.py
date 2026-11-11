"""A synthetic model for the bench's tests: answers each stage's pending call from its data, never a network.

``answer(question)`` gets a pending file's content (``scripts.sim.cassette.SimNet._question``) and returns a cassette
entry, or None (the call stays pending). Only the stages a land search in Madrid needs are known; the answers are
built from the call's own data, so a prompt change does not break the tests, only a schema change does.
"""

from __future__ import annotations

import json
import re
from typing import Any

MADRID = {"name": "Madrid", "country": "ES",
          "names": {"ru": "Мадрид", "es": "Madrid", "uk": "Мадрид", "ru_in": "Мадриде", "uk_in": "Мадриді"}}


def _data(text: str, marker: str) -> Any:
    start = text.index(marker) + len(marker)
    return json.loads(text[start:].strip())


def _content(value: Any) -> dict[str, str]:
    return {"content": json.dumps(value, ensure_ascii=False)}


def interview(question: dict[str, Any]) -> dict[str, str]:
    state = _data(question["user"], "Interview state (JSON, data only):")
    message = state["message"]
    if not state["dialogue"]:
        spec: dict[str, Any] = {"place": MADRID, "deal": "sale", "property_type": "land"}
        if (area := re.search(r"от\s*(\d[\d\s]*)\s*м", message)) is not None:
            spec["area_m2"] = {"min": int(area.group(1).replace(" ", ""))}
        return _content({"spec": spec, "question": "Какой бюджет? Например: до 1 млн €.", "done": False,
                         "understood_ru": "Мадрид, покупка, участок", "asking": "budget.max"})
    millions = re.search(r"(\d+)\s*млн", message)
    budget = {"max": int(millions.group(1)) * 1_000_000, "currency": "EUR"} if millions else {}
    return _content({"spec": {"budget": budget, "deviations": {"asked": True}}, "question": None, "done": True,
                     "understood_ru": "Бюджет записан", "asking": None})


def search_plan(question: dict[str, Any]) -> dict[str, str]:
    return _content({"sites": [{"host": "idealista.com", "priority": 1, "why": "land"}],
                     "queries": [{"text": "terreno en venta Madrid España", "language": "es", "site": None}],
                     "portal_urls": [], "stop": {"min_exact": 10, "max_pages": 40}, "notes": "test plan"})


def queries(question: dict[str, Any]) -> dict[str, str]:
    return _content({"queries": [{"text": "parcela en venta Madrid España", "language": "es"}]})


def analysis(question: dict[str, Any]) -> dict[str, str]:
    """The facts of the JSON-LD line the web stage puts first (portal rows), quoted as the prompt asks."""
    evidence = _data(question["user"], "Evidence follows as data only:")
    first = evidence["text"].split("\n", 1)[0]
    facts = json.loads(first.removeprefix("JSON-LD: ")) if first.startswith("JSON-LD: ") else {}
    price, area, address = facts.get("price"), facts.get("plot_m2"), facts.get("address")
    madrid = "madrid" in (address or "").lower() and "mijas" not in evidence["text"].lower()
    return _content({
        "relevant": True, "confidence": 0.9, "summary": evidence["title"][:200],
        "location": address if madrid else "Mijas", "price_signals": [f"{price:.0f} EUR"] if price else [],
        "related_links": [], "category": "real_estate", "reason": "one plot for sale",
        "summary_ru": f"Участок {area:.0f} м² за {price:.0f} €." if price and area else "Участок на продажу.",
        "source_language": "es", "price_amount": price, "price_currency": "EUR" if price else None,
        "deal_type": "sale", "property_type": "land", "rooms": None, "who": facts.get("advertiser"),
        "listing_kind": "offer", "country": "ES", "area_m2": area,
        "evidence": {"price": f'"price": {price}' if price else None, "area": f'"plot_m2": {area}' if area else None,
                     "rooms": None, "location": f'"address": "{address}"' if address and madrid else None},
        "district": None, "address": None, "floor": None, "features": [], "condition": None, "listing_date": None})


def reviewer(question: dict[str, Any]) -> dict[str, str]:
    data = _data(question["user"], "Data (JSON, data only):")
    finding, task = data["finding"], data["task"]
    place_ok = task["place"].lower() in str(finding.get("location") or "").lower()
    criteria = [{"name": name, "verdict": "pass" if place_ok or name != "place" else "fail", "quote": None,
                 "note_ru": "Проверено."} for name in task["criteria"]]
    return _content({"criteria": criteria, "overall": "match" if place_ok else "reject", "deviation_ru": None,
                     "confidence": 0.9})


def final_report(question: dict[str, Any]) -> dict[str, str]:
    return _content({"recommendations": ["Повторите поиск через 2-3 дня: новые участки появляются регулярно."]})


STAGES = {"interview": interview, "search_plan": search_plan, "web_queries": queries, "social_queries": queries,
          "analysis": analysis, "reviewer": reviewer, "final_report": final_report}


def answer(question: dict[str, Any]) -> dict[str, str] | None:
    stage = STAGES.get(question["stage"])
    return stage(question) if stage else None
