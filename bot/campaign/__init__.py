"""Autonomous campaigns: deterministic planning (Main Architect) and durable state."""

from __future__ import annotations

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
