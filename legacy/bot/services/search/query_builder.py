"""Turn a :class:`ParsedQuery` into concrete search strings.

Deliberately deterministic rather than another LLM call: it is cheap, testable,
and the phrasings that work for property search are well known. The LLM already
did the hard part (understanding the request); this just spells it out for
search engines.

The strategy is one query per (intent template x language), because a listing
in Cyprus is far more likely to be indexed under "οικόπεδο προς πώληση" than
under its English translation.
"""

from __future__ import annotations

from collections.abc import Sequence

from bot.config import SearxngSettings
from bot.models.enums import Mode
from bot.models.query import ParsedQuery, SearchQuery

# Intent phrasings per mode and language. English is the fallback for any
# language we have no translation for -- most listing sites carry an English
# version, so a miss degrades rather than fails.
_TEMPLATES: dict[Mode, dict[str, list[str]]] = {
    Mode.LAND: {
        "en": ["land for sale", "plot for sale", "property for sale"],
        "ru": ["участок продажа", "земельный участок купить", "недвижимость продажа"],
        "uk": ["ділянка продаж", "земельна ділянка купити"],
        "el": ["οικόπεδο προς πώληση", "ακίνητα προς πώληση"],
        "es": ["terreno en venta", "parcela en venta"],
        "it": ["terreno in vendita", "immobili in vendita"],
        "de": ["grundstück kaufen", "immobilien kaufen"],
        "fr": ["terrain à vendre", "immobilier à vendre"],
        "pt": ["terreno à venda", "imóveis à venda"],
        "tr": ["satılık arsa", "satılık arazi"],
        "pl": ["działka na sprzedaż", "nieruchomości na sprzedaż"],
    },
    Mode.INVESTORS: {
        "en": ["real estate investors", "property development company", "investment fund real estate"],
        "ru": ["инвесторы недвижимость", "девелоперская компания", "инвестиционный фонд недвижимость"],
        "uk": ["інвестори нерухомість", "девелоперська компанія"],
        "el": ["επενδυτές ακινήτων", "εταιρεία ανάπτυξης ακινήτων"],
        "es": ["inversores inmobiliarios", "promotora inmobiliaria"],
        "it": ["investitori immobiliari", "società di sviluppo immobiliare"],
        "de": ["immobilieninvestoren", "projektentwickler immobilien"],
        "fr": ["investisseurs immobiliers", "promoteur immobilier"],
        "pt": ["investidores imobiliários", "incorporadora imobiliária"],
        "tr": ["gayrimenkul yatırımcıları", "gayrimenkul geliştirme şirketi"],
        "pl": ["inwestorzy nieruchomości", "deweloper nieruchomości"],
    },
}

_ISO_639_1_LENGTH = 2
"""SearXNG expects 'el' or 'el-GR'; it answers 200 and silently ignores an
unrecognised code, so the only thing worth guarding is the shape -- an LLM that
answers "greek" or "russian" must not turn into a bogus filter."""


#: Used when no settings object is to hand -- keep in step with
#: SearxngSettings.languages, which is the configurable source of truth.
DEFAULT_LANGUAGES = ("en", "es", "ru")


def localized_terms(query: ParsedQuery, *, limit: int) -> list[str]:
    """Short search phrases for *query*, one per language, most useful first.

    Built for search boxes that take a single string -- Facebook's in-group
    search, say -- where the web path's full query set does not fit. Each term
    is the local phrasing plus the location, so a Valencia plot is looked for
    as "terreno en venta Valencia" and not only as whatever language the
    person happened to type their request in.

    Falls back to the raw keywords when the request carries nothing else, so a
    bare "finca rustica" still searches for something.
    """
    languages = _normalised_languages(query.languages, always=DEFAULT_LANGUAGES, limit=limit)
    location = query.location.as_text()
    templates = _TEMPLATES[query.mode]

    terms: list[str] = []
    seen: set[str] = set()
    for language in languages:
        phrasings = templates.get(language) or templates["en"]
        candidate = " ".join(part for part in (phrasings[0], location) if part).strip()
        if candidate and candidate.lower() not in seen:
            seen.add(candidate.lower())
            terms.append(candidate)

    if not terms and query.keywords:
        terms.append(" ".join(query.keywords[:5]))
    return terms[:limit]


def _normalised_languages(
    codes: list[str], *, always: Sequence[str], limit: int
) -> list[str]:
    """The languages to search, detected ones first, *always* guaranteed.

    Order matters: the leading language carries the most weight when hits are
    merged, so a Spanish plot should lead with Spanish rather than with
    whichever code happens to sit first in the configured list. Coverage does
    not depend on order, though -- everything in *always* is included even if
    that means exceeding *limit*, because those languages are a decision
    rather than a guess.

    Codes that are not a bare ISO-639-1 pair are dropped rather than passed
    through, so a model answering "greek" does not become a search filter --
    and is not truncated to "gr", which is a country.
    """
    detected: list[str] = []
    for code in codes:
        normalised = (code or "").strip().lower()
        if len(normalised) == 5 and normalised[2] == "-":
            normalised = normalised[:_ISO_639_1_LENGTH]
        if len(normalised) != _ISO_639_1_LENGTH or not normalised.isalpha():
            continue
        if normalised not in detected:
            detected.append(normalised)

    guaranteed = [code for code in always if code]

    # A detected language that is already guaranteed is not an addition, it is
    # a reordering: it leads, and coverage is unchanged. Only a language the
    # list does not carry -- Greek for a Cyprus request -- spends budget, and
    # it leads too, being the most specific signal about this request.
    extras = [code for code in detected if code not in guaranteed]
    extras = extras[: max(limit - len(guaranteed), 0)]

    ordered = extras + [code for code in detected if code in guaranteed]
    ordered += [code for code in guaranteed if code not in ordered]
    return ordered


def _fit_budget(queries: list[SearchQuery], *, limit: int) -> list[SearchQuery]:
    """Trim to *limit*, giving every language its best query first.

    Sorting by weight and slicing looks right and is not: three languages of
    three phrasings overflow the default budget, so the lowest-weighted
    language lost every slot and was not searched at all. A language that is
    guaranteed up to the point the list is trimmed is not guaranteed.

    So each language claims its strongest query, and whatever budget is left
    is filled by weight as before. Order within the result stays by weight,
    because that is what the merge step reads.
    """
    if len(queries) <= limit:
        return queries

    best_per_language: list[SearchQuery] = []
    claimed: set[str] = set()
    for query in queries:  # already sorted by weight
        if query.language not in claimed:
            claimed.add(query.language)
            best_per_language.append(query)

    kept = best_per_language[:limit]
    for query in queries:
        if len(kept) >= limit:
            break
        if query not in kept:
            kept.append(query)

    kept.sort(key=lambda q: q.weight, reverse=True)
    return kept


class QueryBuilder:
    """Builds the SearXNG query set for one user request."""

    def __init__(self, settings: SearxngSettings) -> None:
        self.settings = settings

    def build(self, query: ParsedQuery) -> list[SearchQuery]:
        """Return up to ``SEARXNG_MAX_QUERIES`` distinct search strings."""
        languages = self._languages(query)
        location = query.location.as_text()
        constraint = self._constraint_terms(query)

        built: list[SearchQuery] = []
        seen: set[str] = set()

        def add(text: str, language: str, weight: float) -> None:
            normalised = " ".join(text.split()).strip()
            if not normalised or normalised.lower() in seen:
                return
            seen.add(normalised.lower())
            built.append(SearchQuery(query=normalised, language=language, weight=weight))

        templates = _TEMPLATES[query.mode]
        for lang_index, language in enumerate(languages):
            phrasings = templates.get(language) or templates["en"]
            # The first phrasing in the first language is our best guess, so it
            # carries the most weight when hits are merged.
            for phrase_index, phrase in enumerate(phrasings):
                weight = 1.0 / (1 + 0.25 * lang_index + 0.2 * phrase_index)
                parts = [phrase, location, query.object_type or "", constraint]
                add(" ".join(p for p in parts if p), language, weight)

        # A keyword-led query catches wording the templates cannot anticipate
        # ("seafront", "with planning permission", a named district).
        if query.keywords:
            keywords = " ".join(query.keywords[:5])
            add(f"{keywords} {location}".strip(), languages[0], 1.1)

        built.sort(key=lambda q: q.weight, reverse=True)
        return _fit_budget(built, limit=self.settings.max_queries)

    def _languages(self, query: ParsedQuery) -> list[str]:
        """Languages to search in, most promising first, always including English.

        Codes that are not a bare ISO-639-1 pair are dropped rather than passed
        through, so a model answering "greek" does not become a search filter.
        """
        return _normalised_languages(
            query.languages,
            always=self.settings.languages or DEFAULT_LANGUAGES,
            limit=self.settings.max_languages,
        )

    def _constraint_terms(self, query: ParsedQuery) -> str:
        """Budget and area as search-friendly text.

        Only the upper bounds are used: engines match these as literal tokens,
        and "up to 300000 EUR" is a phrase that appears on listing pages while
        a range rarely is.
        """
        parts: list[str] = []
        if query.budget_max:
            currency = query.currency or ""
            parts.append(f"{int(query.budget_max):,}".replace(",", " ") + f" {currency}".rstrip())
        if query.area_min and query.area_max:
            parts.append(f"{int(query.area_min)}-{int(query.area_max)} m2")
        elif query.area_max:
            parts.append(f"{int(query.area_max)} m2")
        elif query.area_min:
            # "from 2000 m2" is a floor with no ceiling, which is how people
            # actually ask for land. Rendered as the bare figure for the same
            # reason as the ceiling: engines match it as a token, and pages
            # offering a 2400 m2 plot rarely spell out a range.
            parts.append(f"{int(query.area_min)} m2")
        return " ".join(parts)
