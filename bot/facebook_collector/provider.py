"""Runtime contract for replaceable Facebook acquisition workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    available: bool
    detail: str | None = None


@runtime_checkable
class AcquisitionProvider(Protocol):
    """A provider that executes one durable acquisition batch by its ID."""

    async def run(self, batch_id: str) -> str: ...

    async def health(self) -> ProviderHealth: ...
