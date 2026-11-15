"""Q68: an investor search keeps a foreign-domain hit that names the task's place or Spain (FOREIGN_TLD_SPAIN_MENTION,
off by default). Sim b6-g7-s7: the Angel Investment Network profile «Inmobiliario Business Angel in Puerto Banus-
Marbella (Malaga), España» on angelinvestmentnetwork.ec was dropped before its page was read (``geo.foreign_tld``),
its copy on .com.co passed (``co`` is generic). Mocks only, no network.
"""

from __future__ import annotations

import logging

import pytest

from bot.campaign import MemoryCampaignStore
from bot.web_search.queries import FallbackQueryGenerator, QueryTask
from bot.web_search.searxng import SearchHit
from bot.web_search.settings import WebSearchSettings
from bot.web_search.store import MemoryWebStore
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, names_task_place
from tests.test_investor_web_queries import FixedPlanner, make_campaign
from tests.test_web_search import EVIDENCE, FakeFetcher, FakeSearcher, run_until_done

ANGEL_EC = "https://www.angelinvestmentnetwork.ec/angel-investors/josac-david-a-angel-inversor-puerto-banus-marbella-malaga-spain-343067"
ANGEL_TEXT = ("J A - Inmobiliario Business Angel in Puerto Banus",
              "Inmobiliario Business Angel in Puerto Banus- Marbella (Malaga), España y otros inversores, Marbella "
              "(Malaga), España Nivel de Inversión $70,000 - $116,000,000")
SPAIN_RU = "https://invest-club.example.ru/spain"
SPAIN_TEXT = ("Инвесторы в недвижимость Испании", "Клуб частных инвесторов: проекты в Испании")
OTHER_PL = "https://inwestorzy.example.pl/warszawa"
OTHER_TEXT = ("Aniołowie biznesu Warszawa", "Sieć inwestorów w Polsce")
TITLE_CL = "https://inversionistas.example.cl/angeles"
TITLE_TEXT = ("business angels Málaga", "Red de inversionistas ángeles de Chile")
HOME = "https://www.inversores-malaga.example/club"
TEXTS = {ANGEL_EC: ANGEL_TEXT, SPAIN_RU: SPAIN_TEXT, OTHER_PL: OTHER_TEXT, TITLE_CL: TITLE_TEXT,
         HOME: ("Club de inversores Málaga", "")}


async def _queued(mode: str, *, flag: bool, texts=TEXTS) -> set[str]:
    campaigns, store = MemoryCampaignStore(), MemoryWebStore(None)
    store.campaigns = campaigns
    cid = await make_campaign(campaigns, mode)
    searcher = FakeSearcher(default=list(texts), texts=texts)
    w = WebSearchWorker(campaigns, store, searcher, FakeFetcher(), FallbackQueryGenerator(None), planner=FixedPlanner(),
                        config=WebSearchConfig(cover_portals=False, queries_per_round=6, max_queries_per_campaign=6,
                                               domain_policy="off", investor_web_queries=True,
                                               foreign_tld_spain_mention=flag))
    await run_until_done(w, cid)
    return {r.url for r in store.urls[cid].values()}


async def test_flag_on_an_investor_search_keeps_a_foreign_domain_naming_the_place(caplog) -> None:
    caplog.set_level(logging.INFO, logger="bot.web_search.worker")
    # .ru names only Spain, .pl Warsaw, .cl names Málaga only in its title (the query): dropped
    assert await _queued("investors", flag=True) == {ANGEL_EC, HOME}
    kept = [r for r in caplog.records if r.getMessage() == "web_search.serp_kept_foreign"]
    assert kept and all(r.kept_foreign == 1 for r in kept)


async def test_flag_off_every_foreign_domain_is_dropped_as_before() -> None:
    assert await _queued("investors", flag=False) == {HOME}


async def test_a_real_estate_search_still_drops_a_foreign_domain() -> None:
    listing = "https://pisos-malaga.example.ec/piso-1"
    texts = {listing: ("Piso en venta Málaga, España", "Piso 2 habitaciones 80 m2 por 180.000 € en Málaga"),
             "https://pisos-malaga.example/piso-2": EVIDENCE}
    queued = await _queued("real_estate", flag=True, texts=texts)
    assert "https://pisos-malaga.example/piso-2" in queued and listing not in queued


def _task(location: str = "Málaga", **aliases: str) -> QueryTask:
    return QueryTask(goal="g", task_text="t", location=location, location_aliases=aliases, vertical="investors",
                     country_code="ES")


@pytest.mark.parametrize(("title", "snippet", "named"), [
    ("Business Angel in Puerto Banus", "Marbella (Malaga), España", True),
    ("x", "Business angels Málaga", True),
    ("Business angels Malaga", "", False),  # the title repeats the query
    ("Angel investors", "network in Spain", False),  # the country alone
    ("Инвесторы", "проекты в Испании", False),
    ("x", "Angels Costa del Sol", False),  # a region is not the city or province
    ("Aniołowie biznesu Warszawa", "Sieć inwestorów w Polsce", False),
    ("Angel investors Quito", "Ecuador", False),
])
def test_names_task_place(title, snippet, named) -> None:
    assert names_task_place(_task(es="Málaga", en="Malaga"), SearchHit("https://x.ec/a", title, snippet)) is named


def test_flag_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("FOREIGN_TLD_SPAIN_MENTION", raising=False)
    assert WebSearchConfig().foreign_tld_spain_mention is False
    assert WebSearchSettings(_env_file=None).config().foreign_tld_spain_mention is False
    monkeypatch.setenv("FOREIGN_TLD_SPAIN_MENTION", "true")
    assert WebSearchSettings(_env_file=None).config().foreign_tld_spain_mention is True
