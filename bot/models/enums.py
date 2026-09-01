"""Enumerations that are also persisted in Supabase.

The string values are part of the database contract (they are stored in
``results.status`` and ``feedback.action``), so renaming a value requires a
migration.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """The two research modes the bot offers."""

    LAND = "land"
    """Plots of land, houses, commercial property -- the object itself."""

    INVESTORS = "investors"
    """Investors, funds, developers, agencies -- the counterparty."""

    @property
    def title(self) -> str:
        return {Mode.LAND: "🏡 Участки и объекты", Mode.INVESTORS: "💼 Инвесторы и компании"}[self]


class ResultStatus(StrEnum):
    """Lifecycle of a single result, as seen by one user."""

    NEW = "new"
    SENT = "sent"
    INTERESTING = "interesting"
    NOT_INTERESTING = "not_interesting"
    SAVED = "saved"


class Feedback(StrEnum):
    """What the user pressed under a result."""

    INTERESTING = "interesting"
    NOT_INTERESTING = "not_interesting"
    SAVE = "save"
    DETAILS = "details"

    def to_status(self) -> ResultStatus | None:
        """Status this action moves the result to, or ``None`` if it is a read."""
        return {
            Feedback.INTERESTING: ResultStatus.INTERESTING,
            Feedback.NOT_INTERESTING: ResultStatus.NOT_INTERESTING,
            Feedback.SAVE: ResultStatus.SAVED,
            Feedback.DETAILS: None,
        }[self]
