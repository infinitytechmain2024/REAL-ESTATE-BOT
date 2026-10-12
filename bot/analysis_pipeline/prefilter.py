"""A campaign post that cannot match the task, dropped before the model is paid to read it.

Only certain contradictions count, read deterministically (``bot.utils.listing_text``):

* **deal**: the task wants a sale and the post is plainly a rental (its URL path, the page's JSON-LD, or an offer
  phrase / monthly price in its title), or the reverse;
* **area**: the task has a minimum area and the post states a plot area (or the JSON-LD of a plot of land, or a
  title about a plot) whose largest value is below the floor the campaign would exclude anyway (75 % of the
  minimum, ``tolerance.AREA_SIMILAR_FLOOR``; lower with an approved ``area_pct``).

Anything uncertain goes to the model as before. A dropped post is booked in the cost ledger as a ``skip`` with its
reason, so the final report counts it («отсеяно до ИИ»); nothing disappears without a trace.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from bot.utils.listing_text import deal_of, largest_area, plot_area

from .models import Evidence

AREA_SIMILAR_FLOOR = 0.75  # the same floor as bot.campaign.tolerance (below it a finding is excluded by area)
HEAD_CHARS = 600           # the start of a page: its JSON-LD line and the title block, not the site's other offers
_LAND_TITLE = re.compile(r"(?<!\w)(?:terrenos?|parcelas?|solar(?:es)?|finca\s+r[uú]stica|plot|land|участ\w*|земл\w*)(?!\w)",
                         re.IGNORECASE)
_RENT_PATH = frozenset({"alquiler", "alquilar", "rent", "to-rent", "arrendamiento", "lloguer", "оренда", "аренда"})
_SALE_PATH = frozenset({"venta", "comprar", "compra", "sale", "for-sale", "prodazha", "продаж", "продажа"})


@dataclass(frozen=True, slots=True)
class TaskContext:
    """What a campaign post is checked against: the campaign and the hard numbers of its task."""

    campaign_id: str
    deal: str | None = None            # sale | rent
    property_type: str | None = None   # land | house | apartment | commercial ...
    min_area: float | None = None      # m²
    area_pct: float | None = None      # an approved deviation below the minimum area, in %


def context_of(campaign_id: str, constraints: dict[str, Any] | None, spec: dict[str, Any] | None) -> TaskContext:
    """The context from a campaign's ``plan.constraints`` and ``spec`` (the architect writes ``min_area``)."""
    constraints = constraints if isinstance(constraints, dict) else {}
    spec = spec if isinstance(spec, dict) else {}
    deviations = spec.get("deviations") if isinstance(spec.get("deviations"), dict) else {}
    area = spec.get("area_m2") if isinstance(spec.get("area_m2"), dict) else {}
    deal = constraints.get("deal") or spec.get("deal")
    return TaskContext(
        campaign_id,
        deal if deal in ("sale", "rent") else None,
        str(constraints.get("property_type") or spec.get("property_type") or "") or None,
        _positive(constraints.get("min_area")) or _positive(area.get("min")),
        _positive(deviations.get("area_pct")),
    )


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _jsonld(text: str) -> dict[str, Any]:
    first = text.lstrip().split("\n", 1)[0]
    if not first.startswith("JSON-LD: "):
        return {}
    try:
        data = json.loads(first[len("JSON-LD: "):])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _path_deal(url: str) -> str | None:
    try:
        path = unquote(urlsplit(url).path or "/").lower()
    except ValueError:
        return None
    words = {w for w in re.split(r"[/\-_.]+", path) if w} | set(re.findall(r"(?:for|to)-(?:sale|rent)", path))
    rent, sale = bool(words & _RENT_PATH), bool(words & _SALE_PATH)
    return "rent" if rent and not sale else "sale" if sale and not rent else None


def listing_deal(evidence: Evidence) -> str | None:
    """The post's deal from certain signals only: URL path, JSON-LD ``deal``, then its title."""
    found = _path_deal(evidence.canonical_url or "")
    if found:
        return found
    data = _jsonld(evidence.text)
    if data.get("deal") in ("sale", "rent"):
        return str(data["deal"])
    return deal_of(evidence.title or "")


def listing_area(evidence: Evidence) -> float | None:
    """The largest plot area the post's head states (None: no plot area there, the model decides)."""
    data = _jsonld(evidence.text)
    values = [v for v in (_positive(data.get("plot_m2")),) if v]
    if str(data.get("property_type") or "").lower() == "landparcel" and _positive(data.get("area_m2")):
        values.append(float(data["area_m2"]))
    head = f"{evidence.title}\n{evidence.text[:HEAD_CHARS]}"
    plot = plot_area(head)
    if plot:
        values.append(plot)
    if _LAND_TITLE.search(evidence.title or ""):
        title_area = largest_area(evidence.title or "")
        if title_area:
            values.append(title_area)
    return max(values) if values else None


def prefilter(evidence: Evidence, context: TaskContext | None) -> str | None:
    """``deal`` or ``area`` when the post certainly contradicts the task, else None (the model reads it)."""
    if context is None:
        return None
    if context.deal:
        found = listing_deal(evidence)
        if found and found != context.deal:
            return "deal"
    if context.min_area:
        tolerance = 0.10 if context.area_pct is None else context.area_pct / 100
        floor = min(AREA_SIMILAR_FLOOR, 1 - tolerance - 0.15)
        area = listing_area(evidence)
        if area is not None and area < context.min_area * floor - 1e-9:
            return "area"
    return None
