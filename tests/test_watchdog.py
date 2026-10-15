"""Watchdog: container states, database conditions, cooldown, daily summary and secrets. Mocks only."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from bot.ops.settings import WatchdogSettings
from bot.ops.watchdog import Summary, Watchdog, container_problems, serve

TOKEN = "123456:SECRET-TOKEN-VALUE"
CHAT = 424242
PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"


def settings(**overrides: object) -> WatchdogSettings:
    values: dict[str, object] = {"enabled": True, "owner_chat_id": CHAT, "telegram_token": TOKEN, "database_url": "x",
                                 "docker_socket": "/var/run/docker.sock", "budget_usd": 5.0, "summary_hour": 9}
    values.update(overrides)
    return WatchdogSettings.model_validate(values)


def container(service: str, state: str = "running", status: str = "Up 3 minutes", **labels: str) -> dict:
    return {"Names": [f"/real-estate-monitor-{service}-1"], "State": state, "Status": status,
            "Labels": {PROJECT: "real-estate-monitor", SERVICE: service, **labels}}


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class FakeStore:
    apify: list[str] = field(default_factory=list)
    budget: list[tuple[str, float]] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    portals: list[tuple[str, str]] = field(default_factory=list)
    summary_row: Summary = field(default_factory=lambda: Summary(3, 2, 17, 4, 1.234, 5))
    broken: bool = False
    summary_calls: int = 0

    async def apify_error_streaks(self, since: datetime) -> list[str]:
        if self.broken:
            raise RuntimeError("db down")
        return self.apify

    async def over_budget(self, budget_usd: float) -> list[tuple[str, float]]:
        return self.budget

    async def empty_campaign_streak(self) -> list[str]:
        return self.empty

    async def empty_portals(self, since: datetime) -> list[tuple[str, str]]:
        return self.portals

    async def summary(self, since: datetime) -> Summary:
        self.summary_calls += 1
        return self.summary_row


class Rig:
    def __init__(self, containers: list[dict] | None = None, store: FakeStore | None = None, *,
                 telegram_status: int = 200, at: datetime | None = None, **overrides: object) -> None:
        self.containers = containers or []
        self.messages: list[dict] = []
        self.telegram_urls: list[str] = []
        self.docker_queries: list[dict] = []
        self.telegram_status = telegram_status
        self.clock = Clock(at or datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
        self.store = store or FakeStore()

        def telegram(request: httpx.Request) -> httpx.Response:
            self.telegram_urls.append(str(request.url))
            if self.telegram_status == 200:
                self.messages.append(json.loads(request.content))
            return httpx.Response(self.telegram_status, json={"ok": self.telegram_status == 200})

        def docker(request: httpx.Request) -> httpx.Response:
            self.docker_queries.append(dict(request.url.params))
            return httpx.Response(200, json=self.containers)

        self.dog = Watchdog(settings(**overrides), self.store, httpx.AsyncClient(transport=httpx.MockTransport(telegram)),
                            httpx.AsyncClient(transport=httpx.MockTransport(docker), base_url="http://docker"),
                            clock=self.clock)


async def test_unhealthy_container_alerts_once_and_cooldown_blocks_a_repeat() -> None:
    rig = Rig([container("analysis-worker", status="Up 5 minutes (unhealthy)"), container("postgres")])
    await rig.dog.tick()
    assert len(rig.messages) == 1
    assert "analysis-worker" in rig.messages[0]["text"] and "unhealthy" in rig.messages[0]["text"]
    assert rig.messages[0]["chat_id"] == CHAT
    assert json.loads(rig.docker_queries[0]["filters"]) == {"label": [f"{PROJECT}=real-estate-monitor"]}
    assert rig.docker_queries[0]["all"] == "true"

    rig.clock.advance(minutes=10)
    await rig.dog.tick()
    assert len(rig.messages) == 1  # still cooling down

    rig.clock.advance(minutes=21)  # 31 minutes since the first alert
    await rig.dog.tick()
    assert len(rig.messages) == 2


async def test_exited_and_restarting_containers_alert_one_oneoff_run_is_ignored() -> None:
    rig = Rig([container("campaign-runner", "exited", "Exited (1) 2 minutes ago"),
               container("facebook-collector", "exited", "Exited (0)", **{"com.docker.compose.oneoff": "True"})])
    await rig.dog.tick()
    assert len(rig.messages) == 1
    assert "campaign-runner" in rig.messages[0]["text"] and "exited" in rig.messages[0]["text"]
    assert container_problems([container("searxng", "restarting")]) == [("searxng", "restarting")]


async def test_three_broken_containers_make_one_alert() -> None:
    rig = Rig([container("a", "exited"), container("b", "restarting"), container("c", status="Up (unhealthy)"),
               container("d")])
    await rig.dog.tick()
    assert len(rig.messages) == 1
    text = rig.messages[0]["text"]
    assert "3 сервисов" in text and all(name in text for name in ("a (exited)", "b (restarting)", "c (unhealthy)"))


async def test_a_starting_healthcheck_is_not_an_alert() -> None:
    rig = Rig([container("browser", status="Up 10 seconds (health: starting)")])
    await rig.dog.tick()
    assert rig.messages == []


async def test_empty_docker_socket_skips_containers() -> None:
    rig = Rig([container("a", "exited")], docker_socket="")
    await rig.dog.tick()
    assert rig.messages == [] and rig.docker_queries == []


async def test_three_apify_errors_in_a_row_alert() -> None:
    rig = Rig(store=FakeStore(apify=["aaaaaaaa-1111"]))
    await rig.dog.tick()
    assert len(rig.messages) == 1 and "Apify" in rig.messages[0]["text"] and "aaaaaaaa" in rig.messages[0]["text"]
    await rig.dog.tick()
    assert len(rig.messages) == 1


async def test_spent_budget_alerts() -> None:
    rig = Rig(store=FakeStore(budget=[("bbbbbbbb-2222", 5.25)]))
    await rig.dog.tick()
    assert len(rig.messages) == 1
    assert "бюджет" in rig.messages[0]["text"] and "$5.25" in rig.messages[0]["text"]


async def test_two_empty_campaigns_in_a_row_alert_once_per_process() -> None:
    rig = Rig(store=FakeStore(empty=["c1", "c0"]))
    await rig.dog.tick()
    rig.clock.advance(hours=3)  # far beyond the cooldown: an event is still sent once
    await rig.dog.tick()
    assert len(rig.messages) == 1 and "без единой карточки" in rig.messages[0]["text"]


async def test_portal_with_no_listings_alerts_once() -> None:
    rig = Rig(store=FakeStore(portals=[("dddddddd-4444", "idealista")]))
    await rig.dog.tick()
    rig.clock.advance(hours=2)
    await rig.dog.tick()
    assert len(rig.messages) == 1 and "idealista" in rig.messages[0]["text"] and "0 объявлений" in rig.messages[0]["text"]


async def test_database_failure_does_not_stop_container_alerts() -> None:
    rig = Rig([container("a", "exited")], store=FakeStore(broken=True))
    await rig.dog.tick()
    assert len(rig.messages) == 1


async def test_failed_telegram_send_is_retried_on_the_next_tick() -> None:
    rig = Rig([container("a", "exited")], telegram_status=500)
    await rig.dog.tick()
    assert rig.messages == []
    rig.telegram_status = 200
    await rig.dog.tick()
    assert len(rig.messages) == 1


async def test_daily_summary_once_a_day_at_the_chosen_hour() -> None:
    rig = Rig(at=datetime(2026, 10, 15, 8, 59, tzinfo=UTC))
    await rig.dog.tick()
    assert rig.messages == []  # not yet 09:00
    rig.clock.advance(minutes=1)
    await rig.dog.tick()
    assert len(rig.messages) == 1
    text = rig.messages[0]["text"]
    assert "запущено 3" in text and "завершено 2" in text and "карточек 17" in text and "лидов 4" in text
    assert "$1.23" in text and "ошибок 5" in text
    rig.clock.advance(minutes=30)
    await rig.dog.tick()
    assert len(rig.messages) == 1 and rig.store.summary_calls == 1  # same day: no second summary
    rig.clock.advance(days=1)
    await rig.dog.tick()
    assert len(rig.messages) == 2  # next day at 09:30 is still hour 9


async def test_token_and_chat_id_never_reach_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    rig = Rig([container("a", "exited")], telegram_status=500)
    rig.dog.telegram = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    await rig.dog.tick()  # a failing send
    rig.dog.telegram = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(500)))
    await rig.dog.tick()
    ok = Rig([container("a", "exited")])
    await ok.dog.tick()
    assert TOKEN in ok.telegram_urls[0]  # the token does travel to Telegram ...
    logs = caplog.text
    assert TOKEN not in logs and "SECRET-TOKEN" not in logs and str(CHAT) not in logs  # ... and nowhere else
    assert TOKEN not in repr(rig.dog.settings)


async def test_disabled_watchdog_only_sleeps(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    sleeps: list[float] = []

    async def stop(seconds: float) -> None:
        sleeps.append(seconds)
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        await serve(WatchdogSettings.model_validate({}), sleep=stop)
    assert sleeps == [3600] and "WATCHDOG_ENABLED" in caplog.text


def test_settings_defaults_are_off() -> None:
    s = WatchdogSettings.model_validate({})
    assert (s.enabled, s.owner_chat_id, s.poll_seconds, s.cooldown_minutes, s.summary_hour, s.docker_socket) == (
        False, 0, 60, 30, 9, "")
    assert settings(owner_chat_id=0).missing() == ["OWNER_ALERT_CHAT_ID"]
    assert settings().missing() == []
