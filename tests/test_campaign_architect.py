"""Deterministic campaign planning (no network) and the in-memory campaign store."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bot.campaign import CampaignPlan, InvalidGoal, MemoryCampaignStore, plan_campaign
from bot.campaign.models import CampaignLimits


def test_russian_rent_with_price() -> None:
    plan = plan_campaign("Найди квартиры в аренду в Мадриде до 1200 евро")
    assert plan.location == "Madrid"
    assert plan.vertical == "real_estate"
    assert plan.constraints == {"deal": "rent", "max_price": 1200, "rooms": None}
    assert plan.goal == "real_estate · Madrid · rent ≤ 1200 EUR"
    assert plan.location_aliases == {"es": "Madrid", "en": "Madrid", "ru": "Мадрид", "uk": "Мадрид"}
    assert "аренда квартир Мадрид" in plan.query_seeds["ru"]
    assert "русские в Мадриде жильё" in plan.query_seeds["ru"]
    assert "alquiler pisos Madrid" in plan.query_seeds["es"]


def test_spanish_investors() -> None:
    plan = plan_campaign("Busco inversores para startup en Barcelona")
    assert (plan.location, plan.vertical) == ("Barcelona", "investors")
    assert plan.constraints["deal"] is None
    assert "inversores Barcelona" in plan.query_seeds["es"]
    assert "Barcelona investors" in plan.query_seeds["en"]


def test_english_rent_rooms() -> None:
    plan = plan_campaign("apartments for rent in Valencia 2 bedrooms")
    assert (plan.location, plan.vertical) == ("Valencia", "real_estate")
    assert plan.constraints == {"deal": "rent", "max_price": None, "rooms": 2}
    assert "Valencia apartments for rent" in plan.query_seeds["en"]


def test_ukrainian_rent() -> None:
    plan = plan_campaign("шукаю оренду житла в Мадриді")
    assert (plan.location, plan.vertical, plan.constraints["deal"]) == ("Madrid", "real_estate", "rent")
    assert "оренда житла Мадрид" in plan.query_seeds["uk"]
    assert "українці в Мадриді" in plan.query_seeds["uk"]


def test_both_verticals_merge_within_cap() -> None:
    plan = plan_campaign("Ищу инвесторов и квартиры в Барселоне")
    assert plan.vertical == "both"
    for seeds in plan.query_seeds.values():
        assert len(seeds) == 6
    assert "инвесторы Барселона" in plan.query_seeds["ru"]
    assert "аренда квартир Барселона" in plan.query_seeds["ru"]


@pytest.mark.parametrize(("text", "location"), [
    ("квартира Мадрид", "Madrid"), ("квартира в Мадриді", "Madrid"), ("квартиры в Барселоне", "Barcelona"),
    ("piso en Málaga", "Málaga"), ("piso en malaga", "Málaga"), ("квартира в Малазі", "Málaga"),
    ("rooms in Seville", "Sevilla"), ("квартира в Севилье", "Sevilla"), ("квартира Аліканте", "Alicante"),
    ("alquiler Alacant", "Alicante"), ("квартира в Марбелье", "Marbella"), ("flat in Kiev", "Kyiv"),
    ("квартира в Киеве", "Kyiv"), ("оренда Київ", "Kyiv"), ("оренда в Києві", "Kyiv"),
    ("квартира в Валенсии", "Valencia"), ("оренда у Валенсії", "Valencia"),
])
def test_location_aliases_and_inflections(text: str, location: str) -> None:
    assert plan_campaign(text).location == location


@pytest.mark.parametrize(("text", "vertical"), [
    ("alquiler Madrid", "real_estate"), ("habitación en Madrid", "real_estate"),
    ("аренда Мадрид", "real_estate"), ("нерухомість Мадрид", "real_estate"),
    ("real estate Madrid", "real_estate"), ("investor Madrid", "investors"),
    ("инвестор Мадрид", "investors"), ("інвестори Мадрид", "investors"),
    ("funding for my startup in Madrid", "investors"), ("invest in apartments Madrid", "both"),
])
def test_vertical_detection(text: str, vertical: str) -> None:
    assert plan_campaign(text).vertical == vertical


@pytest.mark.parametrize(("text", "expected"), [
    ("under 1500 eur flat Madrid", {"max_price": 1500}),
    ("piso Madrid hasta 1000€", {"max_price": 1000}),
    ("квартира Мадрид до 1 200 евро", {"max_price": 1200}),
    ("flat Madrid up to 1.5k eur", {"max_price": 1500}),
    ("piso en venta Madrid hasta 250.000€", {"max_price": 250000, "deal": "sale"}),
    ("квартира Мадрид 900€", {"max_price": 900}),
    ("2 комнаты Мадрид аренда", {"rooms": 2, "max_price": None}),
    ("2 habitaciones Madrid alquiler", {"rooms": 2}),
    ("3-х комнатная квартира в Мадриде", {"rooms": 3}),
    ("купить квартиру в Мадриде", {"deal": "sale"}),
    ("квартира в Мадриде до 3 комнат", {"max_price": None}),
])
def test_constraints(text: str, expected: dict[str, object]) -> None:
    constraints = plan_campaign(text).constraints
    for key, value in expected.items():
        assert constraints[key] == value, (text, constraints)


def test_seeds_cover_all_languages_and_are_bounded() -> None:
    for text in ("квартиры в аренду Мадрид", "inversores Sevilla", "investors and flats in Kyiv"):
        plan = plan_campaign(text)
        assert plan.languages == ["es", "en", "ru", "uk"]
        assert set(plan.query_seeds) == {"es", "en", "ru", "uk"}
        for seeds in plan.query_seeds.values():
            assert 1 <= len(seeds) <= 6
            assert all(len(s) <= 80 for s in seeds)
            assert len({s.casefold() for s in seeds}) == len(seeds)
        assert plan.platforms_order == ["facebook_groups", "websites"]


def test_limits_default_and_clamping() -> None:
    plan = plan_campaign("аренда Мадрид")
    assert (plan.limits.max_groups, plan.limits.max_windows, plan.limits.window_size) == (40, 2, 20)
    assert plan_campaign("аренда Мадрид 50 групп").limits.max_groups == 50
    assert plan_campaign("аренда Мадрид 50 групп").limits.max_windows == 3
    big = plan_campaign("rent Madrid 5000 groups").limits
    assert (big.max_groups, big.max_windows) == (200, 10)
    with pytest.raises(ValidationError):
        CampaignLimits(max_groups=40, window_size=21)
    with pytest.raises(ValidationError):
        CampaignLimits(max_groups=201)
    with pytest.raises(ValidationError):
        CampaignLimits(max_groups=40, max_windows=11)


@pytest.mark.parametrize("text", [
    "", "   \n", "аренда Мадрид " * 200, "квартиры в аренду", "Madrid", "asdkj qwpeoi zxmcn",
    "квартиры в Мадриде и Барселоне", "rent in Paris",
])
def test_invalid_goals(text: str) -> None:
    with pytest.raises(InvalidGoal) as info:
        plan_campaign(text)
    assert str(info.value)


def test_plan_json_round_trip_and_strictness() -> None:
    plan = plan_campaign("Найди квартиры в аренду в Мадриде до 1200 евро, 2 комнаты")
    assert CampaignPlan.model_validate_json(plan.model_dump_json()) == plan
    data = plan.model_dump()
    with pytest.raises(ValidationError):
        CampaignPlan.model_validate({**data, "extra": 1})
    with pytest.raises(ValidationError):
        CampaignPlan.model_validate({**data, "query_seeds": {"es": [f"seed {n}" for n in range(7)]}})
    with pytest.raises(ValidationError):
        CampaignPlan.model_validate({**data, "query_seeds": {"es": ["x" * 81]}})
    deduped = CampaignPlan.model_validate({**data, "query_seeds": {"es": ["a b", "A  b", "c"]}})
    assert deduped.query_seeds == {"es": ["a b", "c"]}


async def test_memory_store_transitions() -> None:
    store = MemoryCampaignStore()
    plan = plan_campaign("аренда Мадрид")
    cid = await store.create(plan, chat_id=1, requested_by=2, source_text="аренда Мадрид", actor="telegram:2")
    campaign = await store.get(cid)
    assert campaign and campaign.state == "planned" and campaign.plan == plan
    assert not await store.set_state(cid, "completed", "t")  # planned -> completed is illegal
    assert await store.set_state(cid, "discovering", "t")
    assert await store.set_state(cid, "paused_verification", "t")
    assert await store.set_state(cid, "running", "t")
    assert await store.set_state(cid, "completed", "t", reason="done")
    assert not await store.set_state(cid, "running", "t")  # terminal
    final = await store.get(cid)
    assert final and final.state == "completed" and final.stop_reason == "done" and final.finished_at
    assert await store.set_status_message(cid, 77)
    assert (await store.get(cid)).status_message_id == 77
    assert not await store.set_state("missing", "running", "t")
    with pytest.raises(ValueError):
        await store.set_state(cid, "bogus", "t")
    assert [a[3] for a in store.audit] == ["planned", "discovering", "paused_verification", "running", "completed"]


@pytest.mark.parametrize(("goal", "vertical", "deal"), [
    ("Купить участок в Мадриде до 60 000 €", "real_estate", "sale"),
    ("Куплю землю под Малагой", "real_estate", "sale"),
    ("Купить дом в Валенсии", "real_estate", "sale"),
    ("Земельна ділянка в Києві", "real_estate", None),
    ("Comprar terreno en Madrid", "real_estate", "sale"),
    ("Buy a plot in Marbella", "real_estate", "sale"),
    ("Коммерческое помещение в аренду в Барселоне", "real_estate", "rent"),
    ("Купить в Мадриде до 60 000", "real_estate", "sale"),
    ("Купить участок в Мадриде и найти инвесторов", "both", "sale"),
])
def test_land_houses_and_commercial_property_are_real_estate(goal: str, vertical: str, deal: str | None) -> None:
    plan = plan_campaign(goal)
    assert (plan.vertical, plan.constraints["deal"]) == (vertical, deal)


def test_home_is_a_whole_word_so_pet_groups_stay_off_topic() -> None:
    from bot.campaign.discovery import score_relevance

    assert score_relevance("Домашние животные Валенсия", "", plan_campaign("Купить дом в Валенсии")).relevant is False


def test_only_a_maximum_of_rooms_is_not_a_minimum() -> None:
    from bot.campaign.spec import TaskSpec

    spec = TaskSpec(mode="real_estate").merged({"place": {"name": "Madrid"}, "rooms": {"max": 3}})
    assert plan_campaign("x", vertical="real_estate", spec=spec).constraints["rooms"] is None
    spec = spec.merged({"rooms": {"min": 2}})
    assert plan_campaign("x", vertical="real_estate", spec=spec).constraints["rooms"] == 2
