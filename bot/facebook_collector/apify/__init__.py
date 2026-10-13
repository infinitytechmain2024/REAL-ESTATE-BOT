"""Default-off Apify Facebook acquisition provider."""

from .client import ApifyClient, ApifyClientError, RunHandle, RunStatus
from .mapper import apify_item_to_evidence
from .provider import ApifyBatch, ApifyFacebookConfig, ApifyFacebookProvider

__all__ = [
    "ApifyBatch",
    "ApifyClient",
    "ApifyClientError",
    "ApifyFacebookConfig",
    "ApifyFacebookProvider",
    "RunHandle",
    "RunStatus",
    "apify_item_to_evidence",
]
