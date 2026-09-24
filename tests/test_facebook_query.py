from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.services.facebook.query import group_query, location_matches, post_terms


def _madrid_request() -> ParsedQuery:
    return ParsedQuery(
        mode=Mode.LAND,
        location=Location(city="Madrid", country="Spain"),
        area_min=2000,
        buildable_required=True,
        keywords=["ділянка", "будинок"],
        languages=["uk"],
    )


def test_facebook_group_search_is_location_first_and_localised() -> None:
    query = _madrid_request()

    assert group_query(query) == "terrenos parcelas Madrid"
    assert "ділянка" not in group_query(query)
    terms = post_terms(query, limit=6)
    assert terms[0] == "terreno"
    assert "ділянка" in terms


def test_unrelated_group_title_is_rejected() -> None:
    query = _madrid_request()

    assert location_matches("Bazar Vinnytsia объявления", query) is False
    assert location_matches("Terrenos Madrid y alrededores", query) is True
