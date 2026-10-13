"""Who executes a queued Facebook batch: the seam between the batch queue and an acquisition provider.

``CollectorRunner`` hands a batch id to ``run`` and gets the batch's final state back; any provider that does that
and can say whether it is reachable (``health``) can stand in -- the local browser collector
(``FacebookBatchCollector``, provider ``local``) today, the Apify adapter behind ``FEATURE_APIFY_FACEBOOK`` later.
Whatever the provider, its posts end up in ``collected_posts`` (``bot.acquisition.contracts.AcquiredPost``), so the
analysis, dedup and report never depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    ok: bool
    detail: str = ""   # a short code, safe to log (never a token, cookie or URL with credentials)


@runtime_checkable
class AcquisitionProvider(Protocol):
    name: str

    async def run(self, batch_id: str) -> str: ...

    async def health(self) -> ProviderHealth: ...
