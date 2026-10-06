"""FSM states.

The state machine is intentionally tiny: the mode is the only thing worth
remembering between messages, and it is kept in FSM data (fast, per-chat) as
well as in Supabase (durable, survives a restart).
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Research(StatesGroup):
    """States of one research conversation."""

    choosing_mode = State()
    """/start was sent; waiting for a mode button."""

    waiting_query = State()
    """A mode is set; the next text or voice message is a request."""

    processing = State()
    """A search is running. Further requests are refused until it finishes,
    so one user cannot queue up several expensive pipelines."""
