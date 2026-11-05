"""How one ``Reply`` becomes Telegram messages.

A message carries one markup: inline buttons or the bottom keyboard. A reply with both (``keyboard_note`` set) is
sent as the text with its inline buttons, then the short note with the keyboard, the note only when the keyboard in
that chat changed. Without a note the keyboard of a reply with buttons is dropped (the old behaviour).
"""

from __future__ import annotations

from bot.control_plane.models import Reply


class KeyboardTracker:
    """The bottom keyboard last sent to each chat (in memory: after a restart it is sent again once)."""

    def __init__(self) -> None:
        self._last: dict[int, tuple[tuple[str, ...], ...]] = {}

    def parts(self, chat_id: int, reply: Reply) -> list[Reply]:
        if reply.keyboard is None:
            return [reply]
        if not reply.buttons:
            self._last[chat_id] = reply.keyboard
            return [reply]
        if not reply.keyboard_note:
            return [reply]
        if self._last.get(chat_id) == reply.keyboard:
            return [Reply(reply.text, reply.buttons)]
        self._last[chat_id] = reply.keyboard
        return [Reply(reply.text, reply.buttons), Reply(reply.keyboard_note, keyboard=reply.keyboard)]
