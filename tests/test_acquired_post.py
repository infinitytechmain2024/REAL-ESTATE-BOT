"""T1: the provider-neutral post contract (bot/acquisition/contracts.py), schema v1."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from bot.acquisition.contracts import AcquiredPost, ListingFacts, Phone, Provenance

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
GROUP = "https://www.facebook.com/groups/pisosmadrid"
POST = "https://www.facebook.com/groups/pisosmadrid/posts/1234567890123/"


def source(**kw) -> Provenance:
    data = {"provider": "apify", "actor_id": "owner/fb-groups", "run_id": "aBc123", "group_url": GROUP, "post_url": POST,
            "collected_at": NOW}
    return Provenance(**{**data, **kw})


def post(**kw) -> AcquiredPost:
    return AcquiredPost(**{"campaign_id": "c1", "source": source(), "raw_text": "Vendo parcela de 2.000 m² en Boadilla",
                           **kw})


def test_evidence_requires_currency_with_price() -> None:
    with pytest.raises(ValidationError, match="price_amount without price_currency"):
        ListingFacts(price_amount=300_000)
    assert ListingFacts(price_amount=300_000, price_currency="EUR", deal_type="sale").price_amount == 300_000


def test_evidence_allows_null_fields() -> None:
    p = post()
    facts = p.provider_facts
    assert all(getattr(facts, k) is None for k in ("deal_type", "price_amount", "price_currency", "area_m2",
                                                   "location_text", "latitude", "confidence"))
    assert facts.phones == () and p.quotes == {} and p.schema_version == "1"
    assert source(published_at=None, provider="local", actor_id=None, run_id=None).published_at is None


@pytest.mark.parametrize("bad", [{"location_text": ""}, {"location_text": "   "}, {"price_currency": ""}])
def test_evidence_rejects_empty_string_as_null(bad: dict) -> None:
    with pytest.raises(ValidationError, match="empty string instead of null"):
        ListingFacts(**bad)
    with pytest.raises(ValidationError, match="empty string instead of null"):
        post(quotes={"price": ""})


@pytest.mark.parametrize("bad", [{"price_amount": 0, "price_currency": "EUR"}, {"area_m2": 0}, {"price_currency": "eur"},
                                 {"latitude": 40.4}, {"confidence": 1.5}, {"deal_type": "lease"}])
def test_zero_and_malformed_values_are_refused(bad: dict) -> None:
    with pytest.raises(ValidationError):
        ListingFacts(**bad)


def test_a_rent_price_without_its_period_is_a_warning_not_an_error(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        facts = ListingFacts(deal_type="rent", price_amount=900, price_currency="EUR")
    assert facts.issues() == ["price_period_missing"] and "price_period_missing" in caplog.text
    assert ListingFacts(deal_type="rent", price_amount=900, price_currency="EUR", price_period="month").issues() == []


def test_phones_keep_the_written_form_and_an_optional_e164() -> None:
    facts = ListingFacts(phones=(Phone(raw="600 12 34 56", e164="+34600123456"), Phone(raw="tel. 91 555 00 00")))
    assert [p.raw for p in facts.phones] == ["600 12 34 56", "tel. 91 555 00 00"]
    with pytest.raises(ValidationError):
        Phone(raw="600", e164="600123456")


def test_provenance_rules() -> None:
    with pytest.raises(ValidationError, match="actor_id and run_id"):
        source(run_id=None)
    with pytest.raises(ValidationError, match="http"):
        source(post_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        source(cookies="c_user=1")  # nothing outside the contract, session data least of all


def test_the_collected_posts_row_matches_the_browser_collector() -> None:
    p = post(provider_facts=ListingFacts(deal_type="sale", price_amount=300_000, price_currency="EUR"),
             quotes={"price": "300.000 €", "needs_split": "true"})
    row = p.collected_post_row()
    assert row["platform_post_id"] == "1234567890123" and row["canonical_url"] == POST
    assert row["content_hash"] == hashlib.sha256((POST + "\n" + p.raw_text).encode()).hexdigest()
    assert row["state"] == "normalised" and row["body_text"] == p.raw_text
    assert row["raw_payload"]["provider_facts"] == {"deal_type": "sale", "price_amount": 300000.0, "price_currency": "EUR"}
    assert row["raw_payload"]["run_id"] == "aBc123" and "token" not in str(row).lower()
    other = post(source=source(post_url="https://www.facebook.com/groups/x/?multi_permalinks=99999999"))
    assert other.platform_post_id() == "99999999"
    hashed = post(source=source(post_url="https://www.facebook.com/somewhere"))
    assert hashed.platform_post_id().startswith("url:")


def test_the_contract_is_immutable() -> None:
    p = post()
    with pytest.raises(ValidationError):
        p.campaign_id = "c2"  # type: ignore[misc]
