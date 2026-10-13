"""Contracts for providers that return listing facts without fetching portal HTML."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bot.web_search.queries import QueryTask


@dataclass(frozen=True, slots=True)
class SourceListing:
    """Provider facts; unknown values stay None and plot area stays separate from building area."""

    url: str
    title: str
    price: float | None = None
    currency: str | None = None
    area_m2: float | None = None
    rooms: int | None = None
    address: str | None = None
    property_type: str | None = None
    deal: str | None = None
    description: str = ""
    plot_m2: float | None = None
    phone: str | None = None  # the advertiser's phone as the portal lists it
    advertiser: str | None = None  # agency or owner name as the portal lists it


class ListingSource(Protocol):
    name: str
    hosts: frozenset[str]

    def supports(self, task: QueryTask) -> bool: ...

    async def search(self, task: QueryTask, *, limit: int) -> list[SourceListing]: ...

    async def aclose(self) -> None: ...
