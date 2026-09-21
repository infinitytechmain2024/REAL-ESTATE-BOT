from bot.services.facts import extract_listing_facts


def test_extract_listing_facts_reads_price_area_and_contacts() -> None:
    facts = extract_listing_facts("Parcela 250 000 EUR, 1 200 m2. Marta +34 600 123 456")

    assert facts["price"] == "250 000 EUR"
    assert facts["price_value"] == 250000.0
    assert facts["price_currency"] == "EUR"
    assert facts["area"] == "1 200 m2"
    assert facts["contacts"] == ["+34 600 123 456"]


def test_extract_listing_facts_does_not_invent_missing_values() -> None:
    assert extract_listing_facts("Продаётся участок в Мадриде") == {}
