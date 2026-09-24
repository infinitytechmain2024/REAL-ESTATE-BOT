"""A cheap, deterministic score for ordering candidates before the LLM sees them.

Reading a whole Facebook group produces hundreds of posts, and the ranking call
takes one prompt. Something has to choose which of them it judges, and the
choice must not be "whichever the feed happened to list first".

This is not a judgement -- the LLM still makes that. It is an ordering, built
from what is countable in the text: the place, the wording of the object being
sought, an area, a price, and the words that make a plot worth buying. A post
that mentions nothing scores zero and goes to the back of the queue rather than
being dropped, because a badly written post about the right plot is still about
the right plot.
"""

from __future__ import annotations

import re

from bot.models.query import ParsedQuery
from bot.models.result import PageContent, SearchHit
from bot.utils.places import mentions_place, place_tokens

_AREA = re.compile(r"(\d[\d\s.,]{1,12})\s*(?:m2|m²|кв\.?\s*м|sq\.?\s*m|м2|м²)", re.IGNORECASE)
_PRICE = re.compile(r"[€$£]|\b(?:eur|usd|евро|euros?)\b", re.IGNORECASE)
_BUILDABLE = (
    "urbanizable", "edificable", "buildable", "development", "construccion",
    "construcción", "под застройку", "забудов", "застройк",
)


def names_the_place(hit: SearchHit, content: PageContent | None, query: ParsedQuery) -> bool:
    """Whether the candidate names the place the request asked for.

    Kept apart from the point count and ordered ahead of it: a perfect plot in
    the wrong country is not a near miss, it is a different request, and prompt
    budget spent on it is budget not spent on this one. With no place in the
    request, every candidate is equal on this and the points decide.
    """
    return mentions_place(_text(hit, content), place_tokens(query.location))


def score(hit: SearchHit, content: PageContent | None, query: ParsedQuery) -> float:
    """How much of the request, beyond the place, the text can be seen to match."""
    text = _text(hit, content)
    if not text.strip():
        return 0.0
    lowered = text.lower()
    points = 0.0

    # Imported here: bot.services.facebook reaches back into the pipeline,
    # which imports this module, and a module-level import would close that
    # circle at start-up.
    from bot.services.facebook.query import post_terms

    for term in post_terms(query, limit=6):
        if term.lower() in lowered:
            points += 1.0

    for keyword in query.keywords[:8]:
        if keyword and keyword.lower() in lowered:
            points += 0.5

    if query.object_type and query.object_type.lower() in lowered:
        points += 1.0

    areas = [_area_value(match) for match in _AREA.findall(text)]
    areas = [value for value in areas if value is not None]
    if areas:
        points += 1.0
        if query.area_min is not None and any(value >= query.area_min for value in areas):
            points += 2.0
        if query.area_max is not None and any(value <= query.area_max for value in areas):
            points += 1.0

    if _PRICE.search(text):
        points += 1.0

    if query.buildable_required and any(word in lowered for word in _BUILDABLE):
        points += 2.0

    return points


def most_promising(
    candidates: list[tuple[SearchHit, PageContent | None]],
    query: ParsedQuery,
    *,
    limit: int,
) -> list[tuple[SearchHit, PageContent | None]]:
    """The *limit* best-looking candidates, most promising first.

    Ties keep their original order, which is the order the sources produced
    them: the web hits ranked by the search engines, then the group posts.
    """
    if len(candidates) <= limit:
        return candidates
    ordered = sorted(
        enumerate(candidates),
        key=lambda pair: (
            not names_the_place(pair[1][0], pair[1][1], query),
            -score(pair[1][0], pair[1][1], query),
            pair[0],
        ),
    )
    return [candidate for _index, candidate in ordered[:limit]]


def _text(hit: SearchHit, content: PageContent | None) -> str:
    return " ".join(
        part
        for part in (hit.title, hit.snippet, (content.text if content and content.ok else ""))
        if part
    )


def _area_value(raw: str) -> float | None:
    digits = re.sub(r"[^\d]", "", raw.split(",")[0].split(".")[0])
    return float(digits) if digits else None
