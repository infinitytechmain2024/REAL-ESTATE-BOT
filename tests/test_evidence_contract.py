from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from bot.acquisition.evidence import Evidence, EvidenceSource, ListingFacts


def source(**overrides) -> EvidenceSource:
    values = {
        "provider": "local",
        "actor_id": None,
        "run_id": None,
        "group_url": None,
        "post_url": None,
        "published_at": None,
        "collected_at": datetime.now(UTC),
    }
    values.update(overrides)
    return EvidenceSource(**values)


def test_evidence_requires_currency_with_price() -> None:
    with pytest.raises(ValidationError, match="price_currency"):
        ListingFacts(price_amount=1200)


def test_evidence_allows_null_fields() -> None:
    item = Evidence(
        campaign_id="campaign-1",
        source=source(),
        raw_text="Apartment offered without structured facts",
        listing=ListingFacts(
            listing_kind=None,
            deal_type=None,
            property_type=None,
            price_amount=None,
            price_currency=None,
            price_period=None,
            area_m2=None,
            location_text=None,
            latitude=None,
            longitude=None,
            confidence=None,
        ),
    )

    assert item.schema_version == "1"
    assert item.listing.price_amount is None
    assert item.source.run_id is None


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (lambda: source(actor_id=""), "actor_id"),
        (lambda: ListingFacts(location_text=" "), "location_text"),
        (
            lambda: Evidence(
                campaign_id="campaign-1",
                source=source(),
                raw_text="",
            ),
            "raw_text",
        ),
    ],
)
def test_evidence_rejects_empty_string_as_null(factory, field: str) -> None:
    with pytest.raises(ValidationError) as error:
        factory()
    assert field in str(error.value)


def test_rental_price_without_period_warns() -> None:
    with pytest.warns(UserWarning, match="price_period"):
        facts = ListingFacts(deal_type="rent", price_amount=1200, price_currency="EUR")
    assert facts.price_period is None


def test_phone_preserves_original_and_accepts_optional_e164() -> None:
    facts = ListingFacts(
        phones=["+34 612 345 678", {"original": "612345678", "e164": "+34612345678"}]
    )
    assert facts.phones[0].original == "+34 612 345 678"
    assert facts.phones[0].e164 is None
    assert facts.phones[1].e164 == "+34612345678"


def test_unknown_numeric_facts_use_null_not_zero() -> None:
    with pytest.raises(ValidationError):
        ListingFacts(area_m2=0)
