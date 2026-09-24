from bot.models.result import SearchHit
from bot.services.pipeline import _merge_hits


def test_cross_source_duplicate_keeps_richest_content_and_provenance() -> None:
    merged = _merge_hits(
        [
            SearchHit(
                url="https://example.com/listing?utm_source=facebook",
                title="Land listing",
                snippet="Madrid",
                engines=["facebook_group"],
            ),
            SearchHit(
                url="https://example.com/listing?utm_source=facebook",
                title="",
                snippet="Madrid, 2 000 m2, 250 000 EUR",
                content="full listing text",
                engines=["google", "scrapling"],
            ),
        ]
    )

    assert len(merged) == 1
    assert merged[0].engines == ["facebook_group", "google", "scrapling"]
    assert merged[0].content == "full listing text"
    assert merged[0].snippet.endswith("250 000 EUR")
