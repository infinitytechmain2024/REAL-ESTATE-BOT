"""Group access classification, in both languages the bot will actually meet.

Spain is the v1 market, so a Spanish-rendered group is the normal case, not
an edge one. The rule these tests enforce is that a group we simply failed to
read is reported as ``UNKNOWN_ERROR`` -- never as ``UNAVAILABLE`` and never as
empty. A failed extraction is not evidence that a group is gone or quiet.
"""

from __future__ import annotations

from bot.services.facebook.groups import GroupAccess, check_access
from tests.conftest import FakePage

GROUP = "https://www.facebook.com/groups/example"


async def test_english_join_button_means_membership_required() -> None:
    page = FakePage(url=GROUP, texts={"Join Group": 1})
    assert await check_access(page, GROUP) == GroupAccess.MEMBERSHIP_REQUIRED


async def test_spanish_join_button_means_membership_required() -> None:
    page = FakePage(url=GROUP, texts={"Unirse al grupo": 1})
    assert await check_access(page, GROUP) == GroupAccess.MEMBERSHIP_REQUIRED


async def test_spanish_pending_is_not_mistaken_for_membership() -> None:
    page = FakePage(url=GROUP, texts={"Pendiente": 1})
    assert await check_access(page, GROUP) == GroupAccess.PENDING_APPROVAL


async def test_spanish_unavailable() -> None:
    page = FakePage(url=GROUP, texts={"no está disponible": 1})
    assert await check_access(page, GROUP) == GroupAccess.UNAVAILABLE


async def test_unaccented_spanish_still_matches() -> None:
    """Rollouts differ on accents; both spellings are the same button."""
    page = FakePage(url=GROUP, texts={"no esta disponible": 1})
    assert await check_access(page, GROUP) == GroupAccess.UNAVAILABLE


async def test_spanish_search_box_means_accessible() -> None:
    page = FakePage(url=GROUP, placeholders={"Buscar en este grupo": 1})
    assert await check_access(page, GROUP) == GroupAccess.ACCESSIBLE


async def test_spanish_composer_means_accessible() -> None:
    page = FakePage(url=GROUP, texts={"Escribe algo": 1})
    assert await check_access(page, GROUP) == GroupAccess.ACCESSIBLE


async def test_http_404_is_unavailable() -> None:
    page = FakePage(url=GROUP, response_status=404)
    assert await check_access(page, GROUP) == GroupAccess.UNAVAILABLE


async def test_login_form_beats_any_text() -> None:
    """Structural signals are trusted ahead of localized text."""
    page = FakePage(
        url=GROUP,
        selectors={"#login_form, form[data-testid='royal_login_form']": 1},
        texts={"Join Group": 1},
    )
    assert await check_access(page, GROUP) == GroupAccess.LOGIN_REQUIRED


async def test_unreadable_group_is_unknown_not_unavailable() -> None:
    """The distinction the whole design rests on: failure is not absence."""
    page = FakePage(url=GROUP)
    assert await check_access(page, GROUP) == GroupAccess.UNKNOWN_ERROR
