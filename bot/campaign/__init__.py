"""Autonomous campaigns: deterministic planning (Main Architect) and durable state."""

from __future__ import annotations

from .architect import InvalidGoal, plan_campaign
from .models import Campaign, CampaignLimits, CampaignPlan
from .store import CampaignStore, MemoryCampaignStore, PostgresCampaignStore

__all__ = [
    "Campaign",
    "CampaignLimits",
    "CampaignPlan",
    "CampaignStore",
    "InvalidGoal",
    "MemoryCampaignStore",
    "PostgresCampaignStore",
    "plan_campaign",
]
