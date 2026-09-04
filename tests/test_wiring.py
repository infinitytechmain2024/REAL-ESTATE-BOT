"""Dependency injection wiring.

aiogram matches handler parameters to the dispatcher's context by name and only
finds out at dispatch time that something is missing -- which means a typo in a
new parameter surfaces as a runtime error in front of a user. This checks the
two sides line up while the tests run instead.
"""

from __future__ import annotations

import inspect

import pytest
from conftest import root_router

# Everything `bot.main.build_dispatcher` puts into the dispatcher context, plus
# the names aiogram itself supplies to handlers.
CONTEXT_KEYS = {
    # Injected by build_dispatcher.
    "settings",
    "pipeline",
    "repo",
    "stt",
    "llm",
    "slots",
    "details_cooldown",
    "quota",
    "costs",
    # Supplied by aiogram.
    "bot",
    "bots",
    "state",
    "raw_state",
    "fsm_storage",
    "event_update",
    "event_router",
    "event_from_user",
    "event_chat",
    "event_context",
    "dispatcher",
    "handler",
    "callback_data",
    "command",
    "middleware_data",
    "update",
}


def _handlers():  # type: ignore[no-untyped-def]
    """Every registered handler callback, with the router it came from."""
    router = root_router()
    seen = []

    def walk(node) -> None:  # type: ignore[no-untyped-def]
        for observer in (node.message, node.callback_query):
            for handler in observer.handlers:
                seen.append(handler.callback)
        for child in node.sub_routers:
            walk(child)

    walk(router)
    return seen


@pytest.mark.parametrize("callback", _handlers(), ids=lambda c: c.__name__)
def test_every_handler_parameter_can_be_injected(callback) -> None:  # type: ignore[no-untyped-def]
    """A parameter nobody provides is a crash waiting for a real user."""
    signature = inspect.signature(callback)
    parameters = list(signature.parameters.values())

    # The first positional parameter is the event object itself.
    for parameter in parameters[1:]:
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        assert parameter.name in CONTEXT_KEYS, (
            f"{callback.__name__} asks for {parameter.name!r}, which nothing provides"
        )


def test_the_services_dataclass_covers_the_context() -> None:
    """Every name build_dispatcher injects has to exist on Services."""
    import dataclasses

    from bot.main import Services

    fields = {f.name for f in dataclasses.fields(Services)}
    injected = {"pipeline", "repo", "stt", "llm", "slots", "details_cooldown", "quota", "costs"}
    assert injected <= fields
