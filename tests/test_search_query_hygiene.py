"""v3 Q5: SEARCH_QUERY_HYGIENE -- seeds of the campaign's own deal, neutral land terms, the plan's query languages."""

from __future__ import annotations

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.spec import TaskSpec
from bot.control_plane.settings import ControlPlaneSettings
from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.web_search.queries import QueryTask, TemplateQueryGenerator, kind_terms, land_category
from bot.web_search.settings import WebSearchSettings
from bot.web_search.worker import query_task
from tests.test_orchestra_dispatcher import OPERATORS, FakeStore, claimed

LAND = TaskSpec.model_validate({"mode": "real_estate", "deal": "sale", "property_type": "land",
                                "place": {"name": "Madrid", "country": "ES"}, "area_m2": {"min": 2000}})
GOAL = "покупка Тип: участок. Задача: участок в Мадриде от 2000 м²"
FOREIGN = ("apartments for rent", "rooms for rent", "compartir piso", "alquiler pisos", "flat share",
           "аренда квартир", "снять комнату", "оренда")


def _all(plan) -> list[str]:
    return [seed for seeds in plan.query_seeds.values() for seed in seeds]


def test_hygiene_keeps_only_the_deals_seeds_and_adds_neutral_land_seeds() -> None:
    plan = plan_campaign(GOAL, vertical="real_estate", spec=LAND, query_hygiene=True)
    seeds = _all(plan)
    assert not [s for s in seeds if any(word in s.casefold() for word in FOREIGN)], seeds
    assert plan.query_seeds["es"][:3] == ["terreno Madrid", "parcela Madrid", "solar Madrid"]


def test_without_hygiene_the_other_deals_seeds_stay_last_as_before() -> None:
    plan = plan_campaign(GOAL, vertical="real_estate", spec=LAND)
    assert "Madrid apartments for rent" in plan.query_seeds["en"]
    assert "terreno Madrid" not in plan.query_seeds["es"]


def test_hygiene_both_mode_still_interleaves_the_investor_seeds() -> None:
    plan = plan_campaign("Найди квартиры в аренду в Мадриде и инвесторов", vertical="both", query_hygiene=True)
    assert "inversores Madrid" in plan.query_seeds["es"]
    assert "pisos en venta Madrid" not in plan.query_seeds["es"]


def _land_task(text: str, *, hygiene: bool) -> QueryTask:
    return QueryTask(goal=text, task_text=text, location="Madrid", location_aliases={"es": "Madrid", "en": "Madrid"},
                     vertical="real_estate", constraints={"deal": "sale"}, languages=("es",), query_hygiene=hygiene)


def test_hygiene_land_terms_are_neutral_unless_a_category_is_named() -> None:
    plain = _land_task("участок в Мадриде от 2000 м²", hygiene=True)
    texts = [q.text for q in TemplateQueryGenerator().candidates(plain)]
    assert not [t for t in texts if "urbanizable" in t or "rústica" in t], texts
    assert kind_terms(plain, "land", "es") == ("terreno", "parcela", "solar")
    rustic = _land_task("участок, suelo rústico, Мадрид", hygiene=True)
    assert land_category(rustic) == "rústico"
    assert kind_terms(rustic, "land", "es")[:2] == ("terreno rústico", "parcela rústica")
    assert land_category(_land_task("terreno no urbanizable", hygiene=True)) == "rústico"
    assert land_category(_land_task("parcela urbanizable", hygiene=True)) == "urbanizable"
    assert land_category(_land_task("solar urbano en Madrid", hygiene=True)) == "urbano"


@pytest.mark.parametrize(("text", "category"), [
    ("terreno urbanizable", "urbanizable"),
    ("parcela urbanizable en Madrid", "urbanizable"),
    ("terreno no urbanizable", "rústico"),
    ("suelo no-urbanizable", "rústico"),
    ("no  urbanizable", "rústico"),
    ("no es urbanizable", None),
    ("parcelas que no son urbanizables", None),
    ("no rústico, suelo urbano", "urbano"),
    ("no rústico, urbano", None),
    ("parcela urbana", "urbano"),
    ("terreno con servicios urbanos", None),
    ("plan urbano", None),
    ("desarrollo urbano", None),
    ("vistas urbanas", None),
    ("paisaje urbano", None),
    ("parcela con acceso urbano", None),
    ("terreno urbanizable, no rústico", "urbanizable"),
    ("urbanizable o rústico", None),
    ("solar cerca del casco urbano", None),
])
def test_land_category_reads_negation_and_ambiguity(text: str, category: str | None) -> None:
    assert land_category(_land_task(text, hygiene=True)) == category


def test_without_hygiene_the_land_terms_are_as_before() -> None:
    texts = [q.text for q in TemplateQueryGenerator().candidates(_land_task("участок в Мадриде", hygiene=False))]
    assert any("solar urbanizable" in t for t in texts)


async def test_hygiene_query_languages_come_from_plan_languages() -> None:
    campaigns = MemoryCampaignStore()
    plan = plan_campaign(GOAL, vertical="real_estate", spec=LAND)
    assert plan.languages == ["es", "en", "ru", "uk"]
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text=GOAL, actor="test",
                                 spec=LAND.model_dump(mode="json"))
    campaign = await campaigns.get(cid)
    assert query_task(campaign, hygiene=True).languages == ("es", "en", "ru")
    assert query_task(campaign).languages == ("es", "en", "ru", "uk")


async def test_the_dispatcher_plans_with_the_flag() -> None:
    for hygiene in (False, True):
        campaigns = MemoryCampaignStore()
        store = FakeStore([claimed("campaign", "Найди участок на покупку в Мадриде")])

        async def notify(_chat: int, _text: str) -> None:
            return None

        dispatcher = OrchestraDispatcher(store, operator_ids=OPERATORS, notifier=notify,  # type: ignore[arg-type]
                                         campaigns=campaigns, query_hygiene=hygiene)
        assert await dispatcher.process_once()
        (campaign,) = campaigns.campaigns.values()
        assert ("Madrid apartments for rent" in campaign.plan.query_seeds["en"]) is not hygiene


def test_the_flag_is_off_by_default_in_both_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("SEARCH_QUERY_HYGIENE", raising=False)
    assert ControlPlaneSettings.from_env().search_query_hygiene is False
    assert WebSearchSettings(_env_file=None).config().query_hygiene is False
    monkeypatch.setenv("SEARCH_QUERY_HYGIENE", "true")
    assert ControlPlaneSettings.from_env().search_query_hygiene is True
    assert WebSearchSettings(_env_file=None).config().query_hygiene is True


def test_without_hygiene_both_mode_seeds_are_unchanged() -> None:
    """Golden: the flag off, ``both`` interleaves real estate (other deal last) and investors as before."""
    plan = plan_campaign("Найди квартиры в аренду в Мадриде и инвесторов", vertical="both")
    assert plan.query_seeds["es"] == ["alquiler pisos Madrid", "inversores Madrid", "habitaciones Madrid",
                                      "startups Madrid", "compartir piso Madrid", "emprendedores Madrid"]
