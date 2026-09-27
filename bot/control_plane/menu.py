"""The command menu (the button left of the message field) per audience.

Everyone sees one entry, «Начать» (/start); each owner's private chat gets
the full list. Telegram shows the most specific scope, so the owners' chat
scope overrides the default for them only.
"""

from __future__ import annotations

EVERYONE: tuple[tuple[str, str], ...] = (("start", "Начать"),)
OWNER: tuple[tuple[str, str], ...] = (
    ("start", "Начать"),
    ("settings", "Настройки: роли, доступ, вход в соцсети"),
    ("status", "Статус системы"),
    ("campaign", "Кампания: цель | status | cancel <id>"),
    ("run", "Запустить сбор"),
    ("pause", "Приостановить сбор"),
    ("resume", "Продолжить сбор"),
    ("cancel", "Отменить сбор"),
    ("login", "Вход в Facebook / Instagram / TikTok / LinkedIn"),
    ("auto", "Авто-режим: on | off | status"),
    ("operators", "Список одобренных аккаунтов"),
    ("help", "Все команды"),
)
