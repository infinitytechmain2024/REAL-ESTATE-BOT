"""One real request, carried end to end through query construction.

The message is the operator's own test case, in Ukrainian:

    надай мені земельні участки від 2000 м квадратних з будинками або без,
    в пригороді Мадриду, близкість до метро в 5 хв на машині, участок має
    бути для забудови.

Everything it asks for has to survive into the searches: a **minimum** area
with no maximum, a suburb rather than a city centre, and "for development"
as the thing that makes a plot worth anything here. It also arrives in a
fourth language, which must not change which markets get searched.

The extraction step itself needs a live model, so the ParsedQuery below is
what an LLM is expected to produce from that sentence. Everything after it is
the real code.
"""

from __future__ import annotations

import pytest

from bot.config import SearxngSettings
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.services.search.query_builder import QueryBuilder, localized_terms

REQUEST = (
    "надай мені земельні участки від 2000 м квадратних з будинками або без, "
    "в пригороді Мадриду, близкість до метро в 5 хв на машині, "
    "участок має бути для забудови."
)


@pytest.fixture
def parsed() -> ParsedQuery:
    """What the extraction step should make of REQUEST."""
    return ParsedQuery(
        mode=Mode.LAND,
        location=Location(country="Spain", region="Madrid", raw="пригород Мадрида"),
        object_type="land plot for development",
        area_min=2000,
        languages=["uk"],
        keywords=["buildable", "near metro", "suburb"],
        notes="with or without a house; within 5 minutes' drive of a metro station",
    )


@pytest.fixture
def queries(parsed: ParsedQuery) -> list:
    return QueryBuilder(SearxngSettings()).build(parsed)


def test_the_area_floor_reaches_the_searches(queries) -> None:
    """"від 2000 м²" is a minimum with no maximum, and it was being dropped.

    The builder only rendered a range or an upper bound, so the single
    hardest constraint in the request never reached a search engine -- every
    query looked for plots in Madrid of no particular size.
    """
    assert any("2000" in q.query for q in queries), [q.query for q in queries]


def test_all_three_markets_are_searched(queries) -> None:
    """The guarantee has to survive the query budget, not just the language list.

    Three languages times three phrasings overflows SEARXNG_MAX_QUERIES, and
    truncating by weight alone cut Russian entirely -- so a guaranteed
    language was guaranteed only until the list was trimmed.
    """
    assert {q.language for q in queries} == {"en", "es", "ru"}


def test_the_request_language_does_not_become_a_market(queries) -> None:
    """Ukrainian is how the request arrived, not where the plots are."""
    assert "uk" not in {q.language for q in queries}


def test_madrid_is_in_every_query(queries) -> None:
    assert all("madrid" in q.query.lower() for q in queries), [q.query for q in queries]


def test_the_spanish_queries_use_spanish_words(queries) -> None:
    spanish = " ".join(q.query for q in queries if q.language == "es").lower()

    assert "terreno" in spanish or "parcela" in spanish


def test_the_russian_queries_use_russian_words(queries) -> None:
    russian = " ".join(q.query for q in queries if q.language == "ru").lower()

    assert "участок" in russian or "недвижимость" in russian


def test_what_makes_the_plot_worth_buying_is_not_lost(queries) -> None:
    """Buildable is the point. A plot that cannot be built on is a field."""
    joined = " ".join(q.query for q in queries).lower()

    assert "buildable" in joined or "development" in joined


def test_the_browser_searches_the_same_request_in_three_languages(parsed) -> None:
    terms = " ".join(localized_terms(parsed, limit=3)).lower()

    assert "terreno" in terms
    assert "участок" in terms
    assert "land" in terms
