"""A group reported for a request must be about the place the request named.

Asked for plots in the suburbs of Madrid, the bot answered:

    МОРЕ НЕДВИЖИМОСТЬ В БОЛГАРИИ КУПИТЬ ПРОДАТЬ — Не удалось прочитать группу
    ПРОДАЙ-КУПИ ИМОТ                            — Не удалось прочитать группу
    Kuching property Premium Land posting       — Доступна для чтения

Two Bulgarian groups and one from Malaysia, presented as "groups for your
request". Nothing downstream can catch this: the ranker only ever sees posts,
so a group goes to the user unjudged.

The guard that existed compared the title against city, region and country --
and returned True for everything when all three were empty. That is precisely
the case that needs it: a location the model did not normalise into those
fields is also a location the Facebook search was built without, so the query
degrades to a bare "land property" and brings back the property groups of the
whole world.

Matching also has to cross scripts in both directions. "Madrid" must find
"Недвижимость Мадрида", and "Мадриду" must find "Terrenos Madrid", or the
filter trades one kind of wrong answer for another.
"""

from __future__ import annotations

import pytest

from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.services.facebook.query import group_query, location_matches

#: Verbatim from the incident.
REPORTED = [
    "МОРЕ НЕДВИЖИМОСТЬ В БОЛГАРИИ КУПИТЬ ПРОДАТЬ",
    "ПРОДАЙ-КУПИ ИМОТ",
    "Kuching property Premium Land posting",
]


def _madrid(**location: str) -> ParsedQuery:
    return ParsedQuery(
        mode=Mode.LAND,
        location=Location(**location),
        object_type="land plot for development",
        area_min=2000,
        languages=["uk"],
    )


NORMALISED = _madrid(city="Madrid", region="Madrid", country="Spain")
#: What the model returns when it does not follow "normalise to English".
RAW_ONLY = _madrid(raw="в пригороді Мадриду")


@pytest.mark.parametrize("title", REPORTED)
def test_the_groups_from_the_incident_are_rejected(title: str) -> None:
    assert location_matches(title, NORMALISED) is False


@pytest.mark.parametrize("title", REPORTED)
def test_they_are_rejected_when_the_place_survived_only_in_the_users_wording(title: str) -> None:
    """The unfiltered case: an empty city/region/country used to pass everything."""
    assert location_matches(title, RAW_ONLY) is False


def test_a_spanish_group_about_the_place_is_kept() -> None:
    assert location_matches("Terrenos Madrid y alrededores", NORMALISED) is True


def test_a_russian_language_group_about_the_place_is_kept() -> None:
    """Diaspora groups are the reason Facebook is in this product at all."""
    assert location_matches("Недвижимость в Мадриде и пригородах", NORMALISED) is True


def test_the_users_own_wording_still_finds_the_local_group() -> None:
    """"Мадриду" and "Madrid" are the same place in two scripts and two cases."""
    assert location_matches("Terrenos y parcelas Madrid", RAW_ONLY) is True


def test_a_qualifier_is_not_evidence_of_a_place() -> None:
    """"пригород" appears in the request; a group named for it proves nothing."""
    assert location_matches("Пригород Софии — недвижимость", RAW_ONLY) is False


def test_a_request_that_names_no_place_cannot_judge_a_group() -> None:
    placeless = ParsedQuery(mode=Mode.LAND, keywords=["terreno"])

    assert location_matches("Terrenos en venta", placeless) is True


def test_the_group_query_still_carries_the_place_the_user_typed() -> None:
    assert "Мадриду" in group_query(RAW_ONLY)


async def test_group_discovery_is_skipped_when_the_request_names_no_place(monkeypatch) -> None:
    """Without a place, Facebook's group search returns the world.

    "land property" is what built the incident's group list. Searching it and
    then reporting whatever comes back is worse than saying nothing.
    """
    from unittest.mock import AsyncMock

    from bot.config import FacebookSettings
    from bot.services.facebook import client
    from bot.services.facebook.browser import SessionState
    from tests.conftest import FakePage, FakeSession

    discover = AsyncMock(return_value=[("https://www.facebook.com/groups/x/", "Any group")])
    monkeypatch.setattr(client, "discover_groups", discover)
    session = FakeSession([SessionState.HEALTHY] * 5)
    session.page = FakePage()

    source = client.FacebookSource(FacebookSettings(enabled=True, group_urls=[]), session)
    result = await source.search(ParsedQuery(mode=Mode.LAND, keywords=["terreno"]))

    discover.assert_not_awaited()
    assert result.groups == []
    assert not result.failed
    assert result.notes, "the user is told why Facebook returned nothing"


async def test_publicly_discovered_groups_are_filtered_too(public_source_factory) -> None:
    """The web-search path reports groups as well, and had no filter at all."""
    from bot.models.result import SearchHit

    bulgarian = "https://www.facebook.com/groups/imoti/"
    madrid = "https://www.facebook.com/groups/terrenosmadrid/"
    source, _ = public_source_factory(
        [
            [
                SearchHit(url=bulgarian, title="ПРОДАЙ-КУПИ ИМОТ"),
                SearchHit(url=madrid, title="Terrenos Madrid y alrededores"),
            ],
            [],
            [],
            [],
        ]
    )

    result = await source.search(NORMALISED)

    assert [group.url for group in result.groups] == [madrid]


async def test_an_empty_result_says_when_the_place_was_never_understood(pipeline_factory) -> None:
    """"Попробуйте уточнить локацию" is poor advice for a request that had one.

    When the extraction step returns no place at all, every query afterwards is
    built without one -- so the answer should name that, not ask the user to
    refine a request the bot never read properly.
    """
    from unittest.mock import AsyncMock

    from bot.handlers.search import _send_results
    from bot.services.pipeline import SourceSearchResult
    from tests.conftest import FakeSource

    pipeline, _ = pipeline_factory(FakeSource(SourceSearchResult()))
    pipeline.extract_query.return_value = ParsedQuery(mode=Mode.LAND, keywords=["terreno"])
    pipeline.search.search_many.return_value = []
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="участки под Мадридом")

    status = AsyncMock()
    await _send_results(AsyncMock(), status, outcome, pipeline.settings, Mode.LAND)

    assert "Место в запросе я не распознал" in status.edit_text.call_args.args[0]


async def test_a_located_request_is_not_told_its_place_was_missed(pipeline_factory) -> None:
    from unittest.mock import AsyncMock

    from bot.handlers.search import _send_results
    from bot.services.pipeline import SourceSearchResult
    from tests.conftest import FakeSource

    pipeline, _ = pipeline_factory(FakeSource(SourceSearchResult()))
    pipeline.extract_query.return_value = NORMALISED
    pipeline.search.search_many.return_value = []
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="участки под Мадридом")

    status = AsyncMock()
    await _send_results(AsyncMock(), status, outcome, pipeline.settings, Mode.LAND)

    assert "не распознал" not in status.edit_text.call_args.args[0]


def test_the_country_in_another_language_is_a_known_miss() -> None:
    """"Испания" is not "Spain" to this matcher, and a gazetteer is not worth it.

    Written down so the behaviour is a decision rather than an accident: a
    group that names only the country, in a language other than the one the
    model normalised to, is not shown. The city token carries the common case,
    and the cost of this miss is one group not listed.
    """
    assert location_matches("Недвижимость Испании", NORMALISED) is False
