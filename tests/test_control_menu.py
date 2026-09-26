"""The command menu: «Начать» for everyone, the full list only in owners' chats."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("aiogram")

from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from bot.control_plane import menu
from bot.control_plane.main import _set_menus


class FakeBot:
    def __init__(self, fail_for: int | None = None) -> None:
        self.calls: list[tuple[object, list[str]]] = []
        self.fail_for = fail_for

    async def set_my_commands(self, commands, scope):  # type: ignore[no-untyped-def]
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id == self.fail_for:
            raise RuntimeError("chat not found")
        self.calls.append((scope, [c.command for c in commands]))


def test_commands_follow_telegram_rules() -> None:
    for command, description in (*menu.EVERYONE, *menu.OWNER):
        assert re.fullmatch(r"[a-z0-9_]{1,32}", command) and 1 <= len(description) <= 256
    assert menu.EVERYONE == (("start", "Начать"),)


@pytest.mark.asyncio
async def test_everyone_gets_start_and_owners_the_full_list() -> None:
    bot = FakeBot(fail_for=12)
    await _set_menus(bot, frozenset({11, 12}))  # type: ignore[arg-type]
    default, owner = bot.calls
    assert isinstance(default[0], BotCommandScopeDefault) and default[1] == ["start"]
    assert isinstance(owner[0], BotCommandScopeChat) and owner[0].chat_id == 11
    assert "settings" in owner[1] and "run" in owner[1]
