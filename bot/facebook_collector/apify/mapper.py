"""Map documented Apify Facebook post fields without inferring listing facts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bot.acquisition.evidence import Evidence, EvidenceSource, ListingFacts


def apify_item_to_evidence(
    item: dict[str, Any],
    *,
    campaign_id: str,
    actor_id: str,
    run_id: str,
    group_url: str,
) -> Evidence:
    """Create one Evidence record from one dataset item.

    Listing data is accepted only from explicit structured fields. Free text is left
    for the existing analysis pipeline.
    """
    candidates = item.get("listings")
    needs_split = isinstance(candidates, list) and len(candidates) > 1
    listing = candidates[0] if isinstance(candidates, list) and candidates else item.get("listing", {})
    if not isinstance(listing, dict):
        listing = {}

    raw_quotes = item.get("evidence", {})
    quotes = raw_quotes if isinstance(raw_quotes, dict) else {}
    evidence = {
        str(key): value
        for key, value in quotes.items()
        if isinstance(value, str) and str(key).strip() and value.strip()
    }
    if needs_split:
        evidence["needs_split"] = "true"

    return Evidence(
        campaign_id=campaign_id,
        source=EvidenceSource(
            provider="apify_facebook",
            actor_id=actor_id,
            run_id=run_id,
            group_url=group_url,
            post_url=_text(item.get("url")) or _text(item.get("facebookUrl")),
            published_at=item.get("time") or item.get("timestamp"),
            collected_at=datetime.now(UTC),
        ),
        raw_text=_required_text(item.get("text") or item.get("message")),
        listing=ListingFacts(
            listing_kind=listing.get("listingKind"),
            deal_type=listing.get("dealType"),
            property_type=listing.get("propertyType"),
            price_amount=listing.get("priceAmount"),
            price_currency=listing.get("priceCurrency"),
            price_period=listing.get("pricePeriod"),
            area_m2=listing.get("areaM2"),
            location_text=listing.get("locationText"),
            latitude=listing.get("latitude"),
            longitude=listing.get("longitude"),
            phones=listing.get("phones", []),
            confidence=listing.get("confidence"),
        ),
        evidence=evidence,
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(value: Any) -> str:
    text = _text(value)
    if text is None:
        raise ValueError("apify_item_text_missing")
    return text
