"""Autonomous campaigns: deterministic planning (Main Architect), discovery and the runner.

Names are imported on first use, so a light process (the Telegram control
plane) can plan and store campaigns without loading the discovery stack.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .architect import InvalidGoal, plan_campaign
    from .discovery import (
        CampaignGroup,
        DiscoveryRefused,
        DiscoveryReport,
        DiscoveryStore,
        FacebookDiscovery,
        MemoryDiscoveryStore,
        PostgresDiscoveryStore,
    )
    from .models import Campaign, CampaignLimits, CampaignPlan
    from .store import CampaignStore, MemoryCampaignStore, PostgresCampaignStore

_EXPORTS = {
    "InvalidGoal": "architect", "plan_campaign": "architect",
    "CampaignGroup": "discovery", "DiscoveryRefused": "discovery", "DiscoveryReport": "discovery",
    "DiscoveryStore": "discovery", "FacebookDiscovery": "discovery", "MemoryDiscoveryStore": "discovery",
    "PostgresDiscoveryStore": "discovery",
    "Campaign": "models", "CampaignLimits": "models", "CampaignPlan": "models",
    "CampaignStore": "store", "MemoryCampaignStore": "store", "PostgresCampaignStore": "store",
}

__all__ = [
    "Campaign",
    "CampaignGroup",
    "CampaignLimits",
    "CampaignPlan",
    "CampaignStore",
    "DiscoveryRefused",
    "DiscoveryReport",
    "DiscoveryStore",
    "FacebookDiscovery",
    "InvalidGoal",
    "MemoryCampaignStore",
    "MemoryDiscoveryStore",
    "PostgresCampaignStore",
    "PostgresDiscoveryStore",
    "plan_campaign",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{module}", __name__), name)
