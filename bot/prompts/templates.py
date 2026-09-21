"""The three prompts the pipeline uses: extract, rank, details."""

from __future__ import annotations

import json

from bot.models.enums import Mode
from bot.models.query import ParsedQuery
from bot.models.result import PageContent, SearchHit

_MODE_HINTS: dict[Mode, str] = {
    Mode.LAND: (
        "The user is looking for PROPERTY: land plots, houses, villas, commercial "
        "buildings, warehouses, development sites. Relevant sources are listings, "
        "agency pages, auction and municipality notices, developer project pages."
    ),
    Mode.INVESTORS: (
        "The user is looking for COUNTERPARTIES: investors, funds, family offices, "
        "developers, construction and management companies, brokers. Relevant sources "
        "are company sites, portfolio and 'about us' pages, industry directories, "
        "press releases about deals and funding."
    ),
}

EXTRACT_SYSTEM = """\
You extract structured search intent from a real-estate request.

The user writes freely, in any language, often as a single sentence or a voice
note transcript. Your job is to turn that into search parameters -- not to
answer the request.

Rules:
- Never invent constraints. If the user did not mention a budget, leave it null.
- Normalise the location to English names, but keep the user's own wording in
  `location.raw`.
- Areas are square metres. Convert hectares (x10000), acres (x4047) and
  sotkas (x100). If the user gave a range, fill both bounds.
- `languages` must list the local language of the target country first, then
  "en", then the language the user wrote in. These drive multilingual search.
- `keywords` are the terms worth keeping verbatim in a search query. Do not
  pad them with generic words like "buy" or "property".
- Extract hard constraints separately: minimum area, suburb/location, driving
  time to metro, buildable/development use, and whether a building is optional.
- Also return `criteria`: every condition the user actually stated, classified
  as `required`, `preferred`, or `optional`. Missing information is not a
  failure: never invent a criterion, and never make an unstated parameter
  required. Wording such as "with or without a house" is `optional`.
- `mode` is given to you; keep it unless the text plainly contradicts it.
"""

LOCATION_REPAIR_SYSTEM = """\
You repair geographic location fields for real-estate search. Identify every
place the user explicitly named using world knowledge. Translate inflected or
non-Latin place names to their standard English names. Set city, region and
country whenever they are knowable; never leave city null merely because the
request uses Ukrainian, Russian, Greek or another grammatical form. Preserve
only the exact geographic phrase in `raw`. Do not extract property constraints.

Examples:
- "ділянка біля Мюнхена" -> country Germany, city Munich, raw "біля Мюнхена"
- "house near Λεμεσός" -> country Cyprus, city Limassol, raw "near Λεμεσός"
- "land, at least 2 hectares" -> every location field null
"""

RANK_SYSTEM = """\
You filter and summarise search results for a real-estate researcher.

You receive the user's structured request and a numbered list of candidate
pages with whatever text could be extracted from each. Judge each candidate on
whether it actually serves the request.

Scoring (0-100) -- judge every requested criterion, while treating price as a
separate budget comparison:
  85-100  directly matches: the right kind of object/company, in the right
          location, with the right characteristics
  60-84   plausible match with one characteristic unverified or slightly off
  40-59   relevant context (an agency covering the area, a directory page)
          but not itself the thing requested
  0-39    listing aggregator front pages, unrelated regions, news, spam,
          expired or empty pages

PRICE IS NOT PART OF THE SCORE. A plot in exactly the right place, of exactly
the right kind, that costs twice the stated budget is still an 85+. The caller
compares prices itself and tells the user "nothing in your range, but here is
one 45 000 more" -- which is far more useful than an empty answer. So never
drop, and never mark down, a good match because of its price.

To make that possible, `price_value` and `price_currency` matter:
- `price_value` is a plain number: no spaces, no separators, no symbol.
  "€285,000" is 285000. A range like "from 280k" is 280000.
- `price_currency` is the ISO-4217 code, e.g. EUR, USD, GBP.
- Leave BOTH null when the page states no price. Never estimate one, and never
  carry a price over from a different listing on the same page -- a wrong
  number here is quoted straight back to the user as a difference in euros.

Rules:
- Treat `area_min`, `metro_drive_minutes` and `buildable_required` as hard
  criteria only when the extracted `criteria` marks them `required`. Set
  `criteria_match` false and list each missing or contradicted required
  criterion in `missing_criteria`. Preferred criteria affect ordering but do
  not make a listing invalid. Optional criteria never penalise either form.
- Return close alternatives when no exact matches exist, but label the missing
  criteria instead of presenting them as exact matches.
- Write `summary` in the SAME LANGUAGE the user wrote their request in.
- Summaries are factual and specific: what the object/company is, where, and
  the numbers that appear on the page. Never write marketing copy and never
  claim a fact the extracted text does not support.
- If the page has no usable content, score it on the snippet alone and say so
  in one clause rather than inventing detail.
- `price` and `area` are copied as written, with their units, or left null.
- Return one entry per candidate you consider worth sending, ordered by score,
  and simply omit the rest. Do not return more than the requested maximum.
"""

DETAILS_SYSTEM = """\
You answer a follow-up question about one specific search result.

You are given the user's original request and the text of one page. Produce a
practical briefing in the same language the user used: what this is, the
concrete numbers, who to contact, and what is missing or unclear. Be honest
about gaps -- say "the page does not state the price" rather than guessing.
Keep it under 200 words and use short paragraphs, not bullet symbols.
"""


def build_extract_prompt(text: str, mode: Mode) -> str:
    """User turn for the intent-extraction call."""
    return (
        f"Mode: {mode.value}\n"
        f"{_MODE_HINTS[mode]}\n\n"
        f"User request:\n\"\"\"\n{text.strip()}\n\"\"\"\n\n"
        "Extract the search parameters."
    )


def build_location_repair_prompt(text: str, raw: str | None) -> str:
    """Focused retry when the first extraction kept no normalised place."""
    previous = raw or "(none)"
    return (
        f"User request:\n\"\"\"\n{text.strip()}\n\"\"\"\n\n"
        f"The first extraction preserved this raw location: {previous}\n"
        "Return the corrected location only."
    )


def build_rank_prompt(
    query: ParsedQuery,
    candidates: list[tuple[SearchHit, PageContent | None]],
    *,
    max_results: int,
    max_chars_per_page: int,
) -> str:
    """User turn for the ranking call.

    Each candidate is rendered with its URL, snippet and (when the fetch
    succeeded) the extracted page text, truncated so a long page cannot crowd
    the others out of the context window.
    """
    request_json = json.dumps(query.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2)

    blocks: list[str] = []
    for index, (hit, content) in enumerate(candidates, start=1):
        body = ""
        if content is not None and content.ok:
            body = content.text[:max_chars_per_page]
        elif content is not None and content.error:
            body = f"[page could not be read: {content.error}]"
        else:
            body = "[page not fetched]"

        blocks.append(
            f"### Candidate {index}\n"
            f"URL: {hit.url}\n"
            f"Title: {hit.title or '(none)'}\n"
            f"Author/seller: {hit.author or '(not shown)'}\n"
            f"Search snippet: {hit.snippet or '(none)'}\n"
            f"Found by: {', '.join(hit.engines) or 'unknown'}\n"
            f"Extracted text:\n{body}\n"
        )

    return (
        f"{_MODE_HINTS[query.mode]}\n\n"
        f"User's structured request:\n{request_json}\n\n"
        f"Candidates ({len(candidates)}):\n\n" + "\n".join(blocks) + "\n"
        f"Return at most {max_results} results, best first. Copy each `url` "
        "exactly as given above -- do not shorten, rewrite or invent URLs."
    )


def build_details_prompt(query: ParsedQuery, url: str, title: str, content: str) -> str:
    """User turn for the 'Подробнее' button."""
    return (
        f"Original request: {query.summary()}\n"
        f"Language to answer in: the language of this request text -- "
        f"{query.notes or 'match the user'}\n\n"
        f"Result: {title}\nURL: {url}\n\n"
        f"Page text:\n\"\"\"\n{content[:12000]}\n\"\"\"\n\n"
        "Write the briefing."
    )
