"""Handler routing.

Specifically: which handler claims a voice note in each FSM state. The bug
these cover is that `voice` is registered before `search`, so a voice note
arriving mid-search fell through to the mode-less handler and reset the FSM out
from under the running pipeline.
"""

from __future__ import annotations

import pytest
from conftest import root_router

from bot.handlers import voice
from bot.states import Research


def _voice_handlers() -> list[str]:
    """Names of the voice router's message handlers, in registration order."""
    return [handler.callback.__name__ for handler in voice.router.message.handlers]


def test_the_busy_handler_is_registered_before_the_modeless_one() -> None:
    """Order is what decides the bug: first match wins."""
    names = _voice_handlers()
    assert names.index("on_voice_while_busy") < names.index("on_voice_without_mode")


def test_the_busy_handler_is_filtered_on_the_processing_state() -> None:
    handler = next(
        h for h in voice.router.message.handlers if h.callback.__name__ == "on_voice_while_busy"
    )
    states = [f.callback for f in handler.filters if hasattr(f.callback, "state")]
    assert any(getattr(s, "state", None) == Research.processing for s in states)


def test_the_busy_handler_does_not_touch_the_fsm() -> None:
    """It must not take `state`, so it cannot reset it even by accident."""
    import inspect

    signature = inspect.signature(voice.on_voice_while_busy)
    assert "state" not in signature.parameters


def test_the_modeless_handler_still_sets_the_state() -> None:
    """The legitimate case -- no search running -- must keep working."""
    import inspect

    signature = inspect.signature(voice.on_voice_without_mode)
    assert "state" in signature.parameters


def test_the_router_assembles() -> None:
    assert root_router() is not None


@pytest.mark.parametrize(
    "handler_name",
    ["on_voice", "on_voice_while_busy", "on_voice_without_mode"],
)
def test_every_voice_handler_is_registered(handler_name: str) -> None:
    assert handler_name in _voice_handlers()
