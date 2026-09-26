"""Search inside social networks (TikTok, Instagram, LinkedIn) for a campaign.

The campaign runner's social worker (``worker.SocialSearchWorker``) opens the
network with the owner's logged-in browser profile (``/login <platform>``),
runs the network's OWN search with AI-generated queries (``queries``), reads
the result cards (``adapters``) and files every new post or profile as a
``collected_posts`` row linked to the campaign (``store``), so the ordinary
analysis pipeline and campaign streaming handle it like a Facebook post.
"""

from .adapters import ADAPTERS, SOCIAL_PLATFORMS, Block, SocialItem, adapter_for, detect_block
from .queries import QueryContext, QueryPlanner, SocialQuery, fallback_queries, normalise_query

__all__ = [
    "ADAPTERS",
    "SOCIAL_PLATFORMS",
    "Block",
    "QueryContext",
    "QueryPlanner",
    "SocialItem",
    "SocialQuery",
    "adapter_for",
    "detect_block",
    "fallback_queries",
    "normalise_query",
]
