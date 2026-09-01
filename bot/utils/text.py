"""Text helpers for building Telegram messages."""

from __future__ import annotations

import html
import re

TELEGRAM_MESSAGE_LIMIT = 4096
"""Hard limit imposed by the Bot API on a single ``sendMessage`` text."""

_WHITESPACE_RUN = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def escape_html(value: str) -> str:
    """Escape for Telegram's HTML parse mode.

    Telegram only cares about ``<``, ``>`` and ``&``; escaping quotes as well
    is harmless and keeps us safe if the text is ever put in an attribute.
    """
    return html.escape(value or "", quote=True)


def collapse_whitespace(value: str) -> str:
    """Squeeze runs of spaces and blank lines, preserving paragraph breaks."""
    collapsed = _WHITESPACE_RUN.sub(" ", value or "")
    return _BLANK_LINES.sub("\n\n", collapsed).strip()


def truncate(value: str, limit: int, *, suffix: str = "…") -> str:
    """Cut *value* to *limit* characters on a word boundary when possible."""
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return suffix[:limit]

    window = text[: limit - len(suffix)]
    cut = window.rfind(" ")
    # Only honour the word boundary if it does not throw away most of the text.
    if cut > limit * 0.6:
        window = window[:cut]
    return window.rstrip(" ,.;:—-") + suffix


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split *text* into Telegram-sized chunks, preferring paragraph breaks.

    Splitting happens on ``\\n\\n``, then ``\\n``, then mid-line as a last
    resort, so we never emit a chunk over *limit*.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
