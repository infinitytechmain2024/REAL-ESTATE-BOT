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

_MAX_LANGUAGES = 2
"""More than two languages spends the query budget on breadth over depth."""

_ISO_639_1_LENGTH = 2
"""SearXNG expects 'el' or 'el-GR'; it answers 200 and silently ignores an
unrecognised code, so the only thing worth guarding is the shape -- an LLM that
answers "greek" or "russian" must not turn into a bogus filter."""


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
                # `object_type` comes back from the extractor in English. Glued
                # onto a Spanish or Greek template it makes a half-translated
                # string that matches neither language's pages -- "terreno en
                # venta Madrid land plot" is a worse query than either half.
                object_type = query.object_type if language == "en" else None
                parts = [phrase, location, object_type or "", constraint]
                add(" ".join(p for p in parts if p), language, weight)

        # A keyword-led query catches wording the templates cannot anticipate
        # ("seafront", "with planning permission", a named district).
        #
        # It goes out with no language filter on purpose. The keywords are the
        # one part of the query we did not write, so we cannot vouch for what
        # language they are in: tagging them with `languages[0]` told SearXNG
        # to keep only Spanish pages for a phrase the user wrote in Ukrainian,
        # which reliably returned nothing and burned a query slot.
        if query.keywords:
            keywords = " ".join(query.keywords[:5])
            add(f"{keywords} {location}".strip(), "all", 1.1)

        built.sort(key=lambda q: q.weight, reverse=True)
        return built[: self.settings.max_queries]

    def _languages(self, query: ParsedQuery) -> list[str]:
        """Languages to search in, most promising first, always including English.

        Codes that are not a bare ISO-639-1 pair are dropped rather than passed
        through, so a model answering "greek" does not become a search filter.
        """
        ordered: list[str] = []
        for code in query.languages:
            # Validate before truncating: "greek"[:2] would otherwise become
            # "gr", which is a country, not a language.
            normalised = (code or "").strip().lower()
            if len(normalised) == 5 and normalised[2] == "-":
                normalised = normalised[:_ISO_639_1_LENGTH]
            if len(normalised) != _ISO_639_1_LENGTH or not normalised.isalpha():
                continue
            if normalised not in ordered:
                ordered.append(normalised)
        if "en" not in ordered:
            ordered.append("en")
        return ordered[:_MAX_LANGUAGES]

    def _constraint_terms(self, query: ParsedQuery) -> str:
        """Budget and area as search-friendly text.

        Engines match these as literal tokens, so what goes in is the number as
        a listing page would print it, not a relational phrase: "300 000 EUR"
        appears on pages, "up to 300000 EUR" does not.

        Every bound the user gave is represented. A lower bound alone used to
        be dropped here, which quietly threw away the whole of a request like
        "plots from 2000 m2" -- the area was the most specific thing the user
        said, and none of the queries carried it.
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
            parts.append(f"{int(query.area_min)} m2")
        return " ".join(parts)
