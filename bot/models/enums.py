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


class HitSource(StrEnum):
    """Where a hit came from.

    The bot searches the open web and the operator's Facebook groups for the
    same request and answers from one merged, ranked list -- a listing is a
    listing whoever published it, and splitting the answer by source made the
    user compare two lists by hand. The source survives as a label on the
    individual result so provenance is still visible per listing.
    """

    WEB = "web"
    """A public page found by a search engine through SearXNG."""

    FACEBOOK = "facebook"
    """A post read from a Facebook group the operator's session is a member of."""

    @property
    def badge(self) -> str:
        return {HitSource.WEB: "🌐 Веб-поиск", HitSource.FACEBOOK: "📘 Facebook-группа"}[self]

    @property
    def trusted(self) -> bool:
        """Whether the blocked-domain list should be skipped for this source.

        ``SEARXNG_BLOCKED_DOMAINS`` exists to keep social-network noise out of
        engine results, and it lists facebook.com. A post we read ourselves,
        inside a group we are a member of, is the opposite of noise -- it must
        not be dropped by a filter aimed at drive-by engine hits.
        """
        return self is not HitSource.WEB


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
