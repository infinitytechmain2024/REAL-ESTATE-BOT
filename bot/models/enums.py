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


class BudgetFit(StrEnum):
    """Where a result's price sits relative to the budget the user asked for.

    Computed in code from a numeric price, never asked of the LLM: models are
    unreliable at arithmetic and at deciding what "slightly over" means, and
    the answer has to be exact because it is quoted back to the user.
    """

    EXACT = "exact"
    """Inside the requested range, or no budget was given."""

    OVER = "over"
    """Above the upper bound."""

    UNDER = "under"
    """Below the lower bound -- usually a different segment, not a bargain."""

    UNKNOWN = "unknown"
    """No price on the page, or a currency we cannot compare against."""

    @property
    def is_alternative(self) -> bool:
        """Whether this result should be offered as a near miss rather than a match."""
        return self in (BudgetFit.OVER, BudgetFit.UNDER)


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
