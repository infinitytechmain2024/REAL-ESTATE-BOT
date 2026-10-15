"""T2: the acquisition-provider seam of the Facebook batches (bot/facebook_collector/provider.py)."""

from __future__ import annotations

import pytest

from bot.facebook_collector.collector import FacebookBatchCollector
from bot.facebook_collector.provider import AcquisitionProvider, ProviderHealth
from bot.facebook_collector.runner import CollectorRunner
from tests.test_facebook_batch_collector import FakeBrowser, FakeReader, FakeStore, plan, posts


class HealthyBrowser(FakeBrowser):
    def __init__(self, answer: bool | Exception) -> None:
        super().__init__()
        self.answer = answer

    async def health(self) -> bool:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def local(browser: FakeBrowser, reader: FakeReader | None = None, count: int = 2) -> tuple[FacebookBatchCollector, FakeStore]:
    store = FakeStore(plan(count))
    collector = FacebookBatchCollector(store, browser, reader or FakeReader([posts(1)] * count), max_posts=10,
                                       item_timeout_seconds=10, pause_min_seconds=0, pause_max_seconds=0)
    return collector, store


def test_the_local_collector_is_an_acquisition_provider() -> None:
    collector, _ = local(HealthyBrowser(True))
    assert isinstance(collector, AcquisitionProvider) and collector.name == "local"


@pytest.mark.parametrize(("answer", "health"), [
    (True, ProviderHealth("local", True, "")),
    (False, ProviderHealth("local", False, "browser_unreachable")),
    (RuntimeError("down"), ProviderHealth("local", False, "browser:RuntimeError")),
])
async def test_local_health_reports_and_never_raises(answer, health) -> None:
    collector, _ = local(HealthyBrowser(answer))
    assert await collector.health() == health


async def test_the_runner_runs_any_provider_by_its_run_method() -> None:
    """The batch queue only knows ``run(batch_id)``: a provider swap needs no change in the runner."""

    class Stand:
        name = "apify"

        def __init__(self) -> None:
            self.ran: list[str] = []

        async def run(self, batch_id: str) -> str:
            self.ran.append(batch_id)
            return "succeeded"

        async def health(self) -> ProviderHealth:
            return ProviderHealth(self.name, True)

    from tests.test_facebook_runner import FakeStore as FakeLaunchStore

    stand = Stand()
    assert isinstance(stand, AcquisitionProvider)
    store = FakeLaunchStore(("l1", "b1", "queued"))
    assert await CollectorRunner(store, stand.run).step() is True
    assert stand.ran == ["b1"] and store.finished["l1"] == ("finished", "succeeded", None)


async def test_the_local_provider_still_runs_a_batch_as_before() -> None:
    collector, store = local(HealthyBrowser(True))
    assert await collector.run("batch") == "succeeded"
    assert store.events[-1] == ("batch_finish", "succeeded", None) and len(store.posts) == 2


async def test_join_confirmed_batch_goes_to_the_browser_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """``apify_fallback:join:<group id>`` matches the same prefix as the closed groups' batch -> ``fallback`` -> local."""
    from bot.facebook_collector.main import campaign_provider, choose_provider
    from tests.test_apify_facebook_runner import ON
    from tests.test_apify_facebook_settings import settings

    class Pool:
        sql = ""

        async def fetchval(self, sql: str, batch_id: str):
            Pool.sql = sql
            marker = "apify_fallback:join:5b1f"
            prefix = sql.split("like '")[1].split("%'")[0]
            return "fallback" if marker.startswith(prefix) else "apify"

    assert await campaign_provider(Pool(), "b") == "fallback"  # type: ignore[arg-type]
    s = settings(monkeypatch, APIFY_FACEBOOK_SCOPE="all", **ON)
    assert choose_provider(s, "fallback") == "local"
