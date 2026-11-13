"""v3 Q23: ``suburbs_only`` reads the person's words, not the intake labels (mocks only, no flag: a bug fix).

The N1 run (rent in Madrid, Centro): ``Draft.goal_text`` wrote «Районы или пригороды: Centro.», the word «пригороды»
set ``suburbs_only`` and every portal (Idealista, Fotocasa, Habitaclia, Milanuncios) refused the task.
"""

from __future__ import annotations

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.spec import TaskSpec
from bot.control_plane.intake import Draft
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    ApifyMilanunciosSource,
)
from bot.web_search.worker import query_task, user_words

PORTALS = (ApifyIdealistaPortalSource, ApifyFotocasaSource, ApifyHabitacliaSource, ApifyMilanunciosSource)


def _spec(**place) -> TaskSpec:
    return TaskSpec.model_validate({"mode": "real_estate", "deal": "rent", "property_type": "apartment",
                                    "budget": {"max": 1200, "currency": "EUR"},
                                    "place": {"name": "Madrid", "country": "ES", **place}})


async def _task(text: str, spec: TaskSpec):
    goal = Draft(1, 1, "real_estate", task=text, spec=spec).goal_text()
    campaigns = MemoryCampaignStore()
    plan = plan_campaign(goal, vertical="real_estate", location="Madrid", spec=spec)
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text=goal, actor="test",
                                 spec=spec.model_dump(mode="json"))
    return goal, query_task(await campaigns.get(cid))


@pytest.mark.asyncio
async def test_a_district_from_the_interview_keeps_the_portals():
    goal, task = await _task("квартира в аренду в Мадриде", _spec(districts=["Centro"]))
    assert "Районы или пригороды: Centro." in goal
    assert task.suburbs_only is False
    for portal in PORTALS:
        assert portal("secret-token").supports(task), portal.__name__


@pytest.mark.asyncio
async def test_the_person_asking_for_the_suburbs_only_still_counts():
    _, task = await _task("квартира в аренду, только пригороды Мадрида", _spec(districts=["Getafe"]))
    assert task.suburbs_only is True
    assert not ApifyIdealistaPortalSource("secret-token").supports(task)


def test_every_goal_text_label_is_stripped():
    spec = TaskSpec.model_validate({
        "mode": "both", "deal": "rent", "property_type": "apartment", "budget": {"max": 1200},
        "investor": {"who": ["private"]}, "must_have": ["балкон"], "wishes": [{"text": "метро рядом"}],
        "place": {"name": "Madrid", "country": "ES", "districts": ["Centro"], "radius_km": 10}})
    goal = Draft(1, 1, "both", task="квартира", spec=spec).goal_text()
    left = user_words(goal)
    for label in ("Кого ищем:", "Тип:", "Задача:", "Районы или пригороды:", "Радиус поиска:", "Главное:",
                  "Дополнительно:"):
        assert label in goal and label not in left
    assert "Centro" in left and "квартира" in left
