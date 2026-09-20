"""Session-state classification, and the rule that observing never navigates.

The navigation rule is the important one here. The recovery watcher polls
while a human is typing a password or clearing a checkpoint in that exact
browser window -- in CDP mode it is literally the same tab. A poll that
navigates wipes what they are doing, roughly ninety times over a fifteen
minute incident, and reads to them as Facebook rejecting the login.
"""

from __future__ import annotations

from bot.config import FacebookSettings
from bot.services.facebook.browser import FacebookSession, SessionState
from tests.conftest import LOGGED_IN, SESSION_COOKIES, FakeContext, FakePage


def _session(page: FakePage, cookies: list[str] | None = None) -> FacebookSession:
    """A session wired to fakes, skipping start()."""
    session = FacebookSession(FacebookSettings())
    session._page = page
    session._context = FakeContext(cookie_names=cookies or [])
    return session


# --- the navigation rule ----------------------------------------------------


async def test_observe_state_never_navigates() -> None:
    """The watcher's check must leave the human's page exactly where it is."""
    page = FakePage(url="https://www.facebook.com/login/", selectors={}, body_text="")
    session = _session(page)

    await session.observe_state()

    assert page.goto_calls == [], "observe_state navigated; it must only read"
    assert page.url == "https://www.facebook.com/login/", "the human's page moved"


async def test_probe_state_does_navigate() -> None:
    """The pre-job check is allowed to navigate -- that is the difference."""
    page = FakePage(selectors=LOGGED_IN)
    session = _session(page, SESSION_COOKIES)

    await session.probe_state()

    assert page.goto_calls == ["https://www.facebook.com/"]


async def test_observe_state_reads_the_page_the_human_is_on() -> None:
    """Mid-recovery, a healthy-looking page must be seen without a reload."""
    page = FakePage(url="https://www.facebook.com/groups/feed/", selectors=LOGGED_IN)
    session = _session(page, SESSION_COOKIES)

    assert await session.observe_state() == SessionState.HEALTHY
    assert page.goto_calls == []


# --- classification ---------------------------------------------------------


async def test_checkpoint_url_needs_a_human() -> None:
    page = FakePage(url="https://www.facebook.com/checkpoint/12345/")
    assert await _session(page, SESSION_COOKIES).observe_state() == SessionState.HUMAN_REQUIRED


async def test_login_form_means_login_needed() -> None:
    page = FakePage(
        url="https://www.facebook.com/home.php",
        selectors={"form[data-testid='royal_login_form'], #login_form": 1},
    )
    assert await _session(page).observe_state() == SessionState.LOGIN_NEEDED


async def test_healthy_needs_both_cookie_and_marker() -> None:
    """A session cookie alone is not evidence: checkpoints keep the cookies."""
    page = FakePage(selectors=LOGGED_IN)
    assert await _session(page, []).observe_state() == SessionState.HUMAN_REQUIRED
    assert await _session(FakePage(), SESSION_COOKIES).observe_state() == SessionState.HUMAN_REQUIRED
    assert await _session(page, SESSION_COOKIES).observe_state() == SessionState.HEALTHY


async def test_unknown_layout_fails_safe() -> None:
    """Never guess HEALTHY. An unrecognised page is a human's problem."""
    page = FakePage(url="https://www.facebook.com/something/new/")
    assert await _session(page, SESSION_COOKIES).observe_state() == SessionState.HUMAN_REQUIRED


# --- localisation: the failure mode that alerts nobody ----------------------


async def test_spanish_challenge_text_is_detected() -> None:
    """A Spanish checkpoint must not classify as HEALTHY.

    This is the dangerous direction: English-only matching on a
    Spanish-rendered challenge leaves cookies and nav markers in place, so
    the session reads as healthy and jobs keep running against a challenge
    page -- the one failure that raises no alert at all.
    """
    page = FakePage(
        url="https://www.facebook.com/",
        body_text="Confirma que eres tú para continuar",
        selectors=LOGGED_IN,
    )
    assert await _session(page, SESSION_COOKIES).observe_state() == SessionState.HUMAN_REQUIRED


async def test_english_challenge_text_still_detected() -> None:
    page = FakePage(
        url="https://www.facebook.com/",
        body_text="Please confirm it's you before continuing",
        selectors=LOGGED_IN,
    )
    assert await _session(page, SESSION_COOKIES).observe_state() == SessionState.HUMAN_REQUIRED


async def test_structural_challenge_beats_any_language() -> None:
    """A checkpoint form is a checkpoint whatever language it renders in."""
    page = FakePage(
        url="https://www.facebook.com/",
        selectors={**LOGGED_IN, "form[action*='checkpoint']": 1},
    )
    assert await _session(page, SESSION_COOKIES).observe_state() == SessionState.HUMAN_REQUIRED
