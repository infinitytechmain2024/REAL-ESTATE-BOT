"""The normalized evidence shape passed from acquisition to analysis."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class NormalizedPage:
    """One public page, independent of the acquisition transport.

    It deliberately contains no cookies, request headers, or browser state.
    """

    canonical_url: str
    title: str
    text: str
    platform: str
    source_type: str = "public_page"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)
