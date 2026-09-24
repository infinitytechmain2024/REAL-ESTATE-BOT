"""Searching in more than one language, on both paths.

A plot for sale in Valencia is advertised as "terreno en venta" far more often
than as anything else, but the person asking may have typed in Russian and the
seller may have posted in English. Searching one language finds one slice of
what exists, and which slice is an accident of who typed the request.

The web path has always built one query per (phrasing x language). The Facebook
browser path did not: it searched every group with a single string. These cover
both, plus the language cap that decides how wide either goes.
"""

from __future__ import annotations

from bot.config import SearxngSettings
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.services.search.query_builder import QueryBuilder, localized_terms


def _query(*languages: str, mode: Mode = Mode.LAND) -> ParsedQuery:
    return ParsedQuery(
        mode=mode,
        location=Location(country="Spain", city="Valencia"),
        object_type="land plot",
        languages=list(languages),
        keywords=["terreno"],
    )


# --- how many languages a request reaches ----------------------------------


def test_the_language_cap_is_configurable() -> None:
    """It was a hardcoded 2, which quietly dropped the third language.

    A Russian speaker asking about Spain needs Russian for the diaspora
    groups, Spanish for the local market and English for the listing portals
    that carry an international version. Two of those three is a choice, and
    it should be the operator's.
    """
    parsed = _query("ru", "es")
    built = QueryBuilder(SearxngSettings(max_languages=3, max_queries=20)).build(parsed)

    assert {q.language for q in built} >= {"ru", "es", "en"}


def test_english_is_always_included() -> None:
    built = QueryBuilder(SearxngSettings(max_queries=20)).build(_query("es"))

    assert "en" in {q.language for q in built}


def test_each_language_gets_its_own_phrasing_not_a_translation_of_one() -> None:
    """The point of the templates: local wording, not the English one tagged."""
    built = QueryBuilder(SearxngSettings(max_languages=3, max_queries=20)).build(
        _query("es", "ru")
    )
    text = " ".join(q.query for q in built).lower()

    assert "terreno en venta" in text
    assert "участок" in text
    assert "land for sale" in text


# --- the terms the Facebook browser searches with --------------------------


def test_localized_terms_cover_several_languages() -> None:
    terms = localized_terms(_query("ru", "es"), limit=3)

    joined = " ".join(terms).lower()
    assert "участок" in joined or "недвижимость" in joined
    assert "terreno" in joined
    assert len(terms) == 3


def test_localized_terms_are_distinct() -> None:
    terms = localized_terms(_query("es"), limit=4)

    assert len(terms) == len(set(terms))


def test_localized_terms_respect_the_limit() -> None:
    """Each term is a fresh in-group search: navigation, typing, scrolling.

    Unbounded, a four-language request would drive the shared browser through
    a dozen searches per group.
    """
    assert len(localized_terms(_query("ru", "es", "en"), limit=1)) == 1


def test_localized_terms_fall_back_to_keywords_when_nothing_else_is_known() -> None:
    bare = ParsedQuery(mode=Mode.LAND, keywords=["finca rustica"])

    assert localized_terms(bare, limit=3), "a keyword-only request must still search"


def test_investor_mode_uses_investor_phrasings() -> None:
    terms = localized_terms(_query("es", mode=Mode.INVESTORS), limit=2)

    assert any("inversores" in t.lower() or "investor" in t.lower() for t in terms)


# --- the browser path actually using them ----------------------------------


async def test_the_browser_searches_each_group_in_several_languages(monkeypatch) -> None:
    """One string per group finds one language's worth of posts.

    Spanish sellers post in Spanish, the diaspora groups in Russian, and some
    of both in English. Which one the bot happened to search was decided by
    the language the person typed their request in.
    """
    from unittest.mock import AsyncMock

    from bot.config import FacebookSettings
    from bot.services.facebook import client
    from bot.services.facebook.browser import SessionState
    from bot.services.facebook.groups import GroupAccess, GroupPost
    from tests.conftest import FakePage, FakeSession

    session = FakeSession([SessionState.HEALTHY] * 10)
    session.page = FakePage()
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))
    searched: list[str] = []

    async def fake_search(_page, group_url, term, **_kwargs):
        searched.append(term)
        return [GroupPost(group_url=group_url, post_url=f"{group_url}/p/{len(searched)}", text="x")]

    monkeypatch.setattr(client, "search_posts", fake_search)

    source = client.FacebookSource(
        FacebookSettings(enabled=True, group_urls=["https://www.facebook.com/groups/g"]),
        session,
    )
    await source.search(_query("ru", "es"))

    joined = " ".join(searched).lower()
    assert len(searched) > 1, f"searched only once: {searched}"
    assert "terreno" in joined, searched
    assert "участок" in joined or "недвижимость" in joined, searched


async def test_the_same_post_found_twice_is_reported_once(monkeypatch) -> None:
    """Two languages will surface overlapping posts; the user sees one."""
    from unittest.mock import AsyncMock

    from bot.config import FacebookSettings
    from bot.services.facebook import client
    from bot.services.facebook.browser import SessionState
    from bot.services.facebook.groups import GroupAccess, GroupPost
    from tests.conftest import FakePage, FakeSession

    session = FakeSession([SessionState.HEALTHY] * 10)
    session.page = FakePage()
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))

    async def same_post(_page, group_url, _term, **_kwargs):
        return [GroupPost(group_url=group_url, post_url=f"{group_url}/p/1", text="same")]

    monkeypatch.setattr(client, "search_posts", same_post)

    source = client.FacebookSource(
        FacebookSettings(enabled=True, group_urls=["https://www.facebook.com/groups/g"]),
        session,
    )
    result = await source.search(_query("ru", "es"))

    assert len(result.hits) == 1, [h.url for h in result.hits]


# --- the three we always search --------------------------------------------


def test_the_configured_languages_are_always_searched() -> None:
    """English, Spanish and Russian, whatever the request looked like.

    Neither the account's interface language nor the language the person
    typed in should decide which listings exist. A Spanish seller, a Russian
    diaspora group and an English portal listing are all the same plot.
    """
    built = QueryBuilder(SearxngSettings(max_queries=20)).build(_query())

    assert {q.language for q in built} >= {"en", "es", "ru"}


def test_a_request_with_no_detected_language_still_searches_all_three() -> None:
    bare = ParsedQuery(mode=Mode.LAND, location=Location(city="Valencia"), keywords=["terreno"])
    built = QueryBuilder(SearxngSettings(max_queries=20)).build(bare)

    assert {q.language for q in built} >= {"en", "es", "ru"}


def test_the_detected_language_still_leads() -> None:
    """Coverage is guaranteed; order is still the best guess first.

    The leading language carries the most weight when hits are merged, and
    for a Spanish plot that should be Spanish rather than whichever language
    happens to sit first in the configured list.
    """
    built = QueryBuilder(SearxngSettings(max_queries=20)).build(_query("es"))

    assert built[0].language == "es"


def test_a_local_language_is_picked_up_when_the_budget_allows() -> None:
    """Cyprus is advertised in Greek, which is not one of the three."""
    cyprus = ParsedQuery(
        mode=Mode.LAND, location=Location(country="Cyprus"), languages=["el"], keywords=["plot"]
    )
    built = QueryBuilder(SearxngSettings(max_languages=4, max_queries=20)).build(cyprus)

    assert {q.language for q in built} >= {"el", "en", "es", "ru"}


def test_the_browser_terms_cover_the_same_three() -> None:
    terms = " ".join(localized_terms(_query(), limit=3)).lower()

    assert "land for sale" in terms
    assert "terreno" in terms
    assert "участок" in terms
