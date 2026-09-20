"""Fakes for the Playwright objects the Facebook code drives.

Real browser automation cannot be unit-tested, but the *decisions* around it
can: which state a page classifies as, whether a poll navigates, whether two
callers serialise. These fakes model just enough of Playwright's surface for
that, and deliberately record what was done to them so a test can assert on
it -- ``FakePage.goto_calls`` is how "the watcher must not navigate" becomes
an assertion instead of a code review comment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from bot.services.facebook.browser import SessionState


class FakeLocator:
    """A locator that knows only how many things it matched."""

    def __init__(self, count: int = 0, attribute: str | None = None, text: str = "") -> None:
        self._count = count
        self._attribute = attribute
        self._text = text
        self.click_calls = 0

    async def count(self) -> int:
        return self._count

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, _index: int) -> FakeLocator:
        return self

    async def click(self) -> None:
        self.click_calls += 1

    async def fill(self, _value: str) -> None:
        return None

    async def get_attribute(self, _name: str) -> str | None:
        return self._attribute

    async def inner_text(self, timeout: float | None = None) -> str:
        return self._text


@dataclass
class FakeResponse:
    status: int = 200


class FakePage:
    """A page whose content is whatever the test says it is.

    ``selectors`` maps a selector string to the number of matches; ``texts``
    is the visible body text. Anything not listed matches zero elements,
    which is what an unrecognised layout looks like in real life.
    """

    def __init__(
        self,
        url: str = "https://www.facebook.com/",
        body_text: str = "",
        selectors: dict[str, int] | None = None,
        texts: dict[str, int] | None = None,
        placeholders: dict[str, int] | None = None,
        response_status: int = 200,
        closed: bool = False,
    ) -> None:
        self.url = url
        self._body_text = body_text
        self._selectors = selectors or {}
        self._texts = texts or {}
        self._placeholders = placeholders or {}
        self._response_status = response_status
        self._closed = closed
        #: Every URL this page was navigated to. The point of the fake.
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **_kwargs: object) -> FakeResponse:
        self.goto_calls.append(url)
        self.url = url
        return FakeResponse(status=self._response_status)

    def locator(self, selector: str) -> FakeLocator:
        if selector == "body":
            return FakeLocator(count=1, text=self._body_text)
        return FakeLocator(count=self._selectors.get(selector, 0))

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(count=self._texts.get(text, 0))

    def get_by_placeholder(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(count=self._placeholders.get(text, 0))

    def is_closed(self) -> bool:
        return self._closed

    def set_default_navigation_timeout(self, _ms: float) -> None:
        return None


@dataclass
class FakeContext:
    """Just enough context to answer "are the session cookies there?"."""

    cookie_names: list[str] = field(default_factory=list)
    pages: list[FakePage] = field(default_factory=list)

    async def cookies(self) -> list[dict[str, str]]:
        return [{"name": name} for name in self.cookie_names]

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        return None


LOGGED_IN = {"[aria-label='Facebook'], div[role='navigation']": 1}
SESSION_COOKIES = ["c_user", "xs"]


@pytest.fixture(autouse=True)
def _minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings validate at construction; give them the one required value.

    Nothing here reaches the network -- the token is a syntactically valid
    placeholder so `Settings()` can be built in a test at all.
    """
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:test-token-not-real")


class FakeSession:
    """Records whether the lock was held at the moment state was observed."""

    def __init__(self, states: list[SessionState]) -> None:
        self._states = list(states)
        self.has_live_context = True
        self.start_calls = 0
        self.lock = asyncio.Lock()
        self.observed_unlocked = 0
        self.probe_calls = 0
        self.observe_calls = 0

    async def observe_state(self) -> SessionState:
        self.observe_calls += 1
        if not self.lock.locked():
            self.observed_unlocked += 1
        return self._states.pop(0) if self._states else SessionState.HUMAN_REQUIRED

    async def probe_state(self) -> SessionState:
        self.probe_calls += 1
        if not self.lock.locked():
            self.observed_unlocked += 1
        return self._states.pop(0) if self._states else SessionState.HUMAN_REQUIRED

    async def start(self) -> None:
        self.start_calls += 1


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, _chat_id: int, text: str, **_kwargs: object) -> None:
        self.messages.append(text)


class FakeTokenStore:
    def __init__(self) -> None:
        self.invalidated = 0

    async def invalidate(self) -> None:
        self.invalidated += 1

    async def get_or_create(self, _ttl: int) -> str:
        return "tok"


class FakeIncidentRepo:
    def __init__(self, current=None):
        self.current = current
        self.opened = []
        self.resolved = []

    async def current_facebook_incident(self):
        return self.current

    async def open_facebook_incident(self, state):
        from uuid import uuid4

        incident_id = uuid4()
        self.opened.append(state)
        self.current = {"id": str(incident_id), "state": state}
        return incident_id

    async def resolve_facebook_incident(self, incident_id):
        self.resolved.append(incident_id)
        self.current = None
