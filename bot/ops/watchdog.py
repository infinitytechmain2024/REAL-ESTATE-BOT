"""Watchdog: tells the owner in Telegram when a service or a campaign goes wrong, and sends a daily summary.

Every ``WATCHDOG_POLL_SECONDS`` it looks at
  * the project's containers through the Docker Engine API (``WATCHDOG_DOCKER_SOCKET``, a read-only mount of
    ``/var/run/docker.sock``; empty: skipped): ``exited``, ``restarting`` or ``unhealthy``;
  * the database: three Apify errors in a row, a campaign over ``CAMPAIGN_BUDGET_USD``, two finished campaigns in a
    row with no cards, a portal that returned no listings.
Alerts go to ``OWNER_ALERT_CHAT_ID`` through ``sendMessage``. The same key is not repeated within
``WATCHDOG_ALERT_COOLDOWN_MINUTES`` (kept in memory: after a restart one repeat is possible); one-off events
(an empty portal, empty campaigns) are sent once per process. The summary goes out once a day at
``WATCHDOG_DAILY_SUMMARY_HOUR`` (UTC). Off by default (``WATCHDOG_ENABLED=false``: the process only logs and sleeps).
The Telegram token and the chat id are never logged (httpx request logging is muted: its URL holds the token).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from .settings import WatchdogSettings

log = logging.getLogger("bot.ops.watchdog")

COMPOSE_PROJECT = "real-estate-monitor"
BAD_STATES = ("exited", "restarting")
MANY_SERVICES = 3  # this many broken containers at once are reported as one alert
APIFY_ERRORS_IN_A_ROW = 3
EMPTY_CAMPAIGNS_IN_A_ROW = 2
TELEGRAM_API = "https://api.telegram.org"


def mute_http_logs() -> None:
    """httpx logs every request line at INFO, and a Telegram request URL contains the bot token."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


@dataclass(frozen=True, slots=True)
class Alert:
    key: str
    text: str
    once: bool = False  # never repeated in this process (an event, not a state)


@dataclass(frozen=True, slots=True)
class Summary:
    campaigns_started: int = 0
    campaigns_finished: int = 0
    cards: int = 0
    leads: int = 0
    cost_usd: float = 0.0
    errors: int = 0


class Store(Protocol):
    async def apify_error_streaks(self, since: datetime) -> list[str]: ...
    async def over_budget(self, budget_usd: float) -> list[tuple[str, float]]: ...
    async def empty_campaign_streak(self) -> list[str]: ...
    async def empty_portals(self, since: datetime) -> list[tuple[str, str]]: ...
    async def summary(self, since: datetime) -> Summary: ...


class PostgresStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def apify_error_streaks(self, since: datetime) -> list[str]:
        """Campaigns whose last three Apify (stage ``api``) ledger rows are all errors."""
        rows = await self.pool.fetch(
            """select campaign_id::text from (
                   select campaign_id, kind, row_number() over (partition by campaign_id order by id desc) as rn
                   from campaign_costs
                   where stage = 'api' and kind in ('cost', 'error') and campaign_id is not null and created_at >= $1
               ) t where rn <= $2 group by campaign_id having count(*) = $2 and bool_and(kind = 'error')""",
            since, APIFY_ERRORS_IN_A_ROW)
        return [r[0] for r in rows]

    async def over_budget(self, budget_usd: float) -> list[tuple[str, float]]:
        """Running campaigns (or finished in the last day) whose paid calls reached the budget; 0 = no limit."""
        if budget_usd <= 0:
            return []
        rows = await self.pool.fetch(
            """select c.id::text, sum(k.cost_usd)::float8 as spent
               from campaigns c join campaign_costs k on k.campaign_id = c.id and k.kind = 'cost'
               where c.state not in ('completed', 'cancelled', 'failed') or c.updated_at >= now() - interval '1 day'
               group by c.id having sum(k.cost_usd) >= $1""", budget_usd)
        return [(r[0], float(r[1])) for r in rows]

    async def empty_campaign_streak(self) -> list[str]:
        """Ids of the last two completed campaigns when neither sent a single card, else an empty list."""
        rows = await self.pool.fetch(
            """select c.id::text, (select count(*) from campaign_findings f
                                   where f.campaign_id = c.id and f.state = 'sent') as cards
               from campaigns c where c.state = 'completed'
               order by c.finished_at desc nulls last, c.created_at desc limit $1""", EMPTY_CAMPAIGNS_IN_A_ROW)
        if len(rows) == EMPTY_CAMPAIGNS_IN_A_ROW and all(r[1] == 0 for r in rows):
            return [r[0] for r in rows]
        return []

    async def empty_portals(self, since: datetime) -> list[tuple[str, str]]:
        rows = await self.pool.fetch(
            """select campaign_id::text, name from web_listing_source_runs
               where state = 'completed' and jsonb_array_length(listings) = 0 and updated_at >= $1""", since)
        return [(r[0], r[1]) for r in rows]

    async def summary(self, since: datetime) -> Summary:
        row = await self.pool.fetchrow(
            """select
                 (select count(*) from campaigns where created_at >= $1) as started,
                 (select count(*) from campaigns where finished_at >= $1) as finished,
                 (select count(*) from campaign_findings where state = 'sent' and sent_at >= $1) as cards,
                 (select count(*) from investor_leads where created_at >= $1) as leads,
                 (select coalesce(sum(cost_usd), 0)::float8 from campaign_costs
                   where kind = 'cost' and created_at >= $1) as cost,
                 (select coalesce(sum(units), 0) from campaign_costs
                   where kind = 'error' and created_at >= $1) as errors""", since)
        return Summary(int(row["started"]), int(row["finished"]), int(row["cards"]), int(row["leads"]),
                       float(row["cost"]), int(row["errors"]))


def container_problems(containers: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(service, problem) for each broken container of the compose project; one-off ``run`` containers are skipped."""
    problems: list[tuple[str, str]] = []
    for item in containers:
        labels = item.get("Labels") or {}
        if str(labels.get("com.docker.compose.oneoff", "False")).lower() == "true":
            continue
        service = labels.get("com.docker.compose.service") or (item.get("Names") or ["?"])[0].lstrip("/")
        state = str(item.get("State", "")).lower()
        if state in BAD_STATES:
            problems.append((service, state))
        elif state == "running" and "(unhealthy)" in str(item.get("Status", "")):
            problems.append((service, "unhealthy"))
    return sorted(problems)


def container_alerts(problems: list[tuple[str, str]]) -> list[Alert]:
    if not problems:
        return []
    if len(problems) >= MANY_SERVICES:
        listing = ", ".join(f"{service} ({problem})" for service, problem in problems)
        return [Alert("containers:many", f"Проблемы у {len(problems)} сервисов: {listing}. "
                                         "Смотри: docker compose ps; docker compose logs --tail=50")]
    return [Alert(f"container:{service}:{problem}",
                  f"Сервис {service}: {problem}. Смотри: docker compose logs --tail=50 {service}")
            for service, problem in problems]


class Watchdog:
    def __init__(self, settings: WatchdogSettings, store: Store | None, telegram: httpx.AsyncClient,
                 docker: httpx.AsyncClient | None, *, clock: Callable[[], datetime] | None = None) -> None:
        mute_http_logs()
        self.settings = settings
        self.store = store
        self.telegram = telegram
        self.docker = docker
        self.clock = clock or (lambda: datetime.now(UTC))
        self._sent: dict[str, datetime] = {}
        self._summary_day: str | None = None

    # -- delivery -------------------------------------------------------------------------------------------------
    async def send(self, text: str) -> bool:
        try:
            response = await self.telegram.post(
                f"{TELEGRAM_API}/bot{self.settings.telegram_token}/sendMessage",
                json={"chat_id": self.settings.owner_chat_id, "text": text, "disable_web_page_preview": True})
            if response.status_code == 200:
                return True
            log.warning("telegram sendMessage refused: http %s", response.status_code)
        except Exception as exc:  # noqa: BLE001 - the message of an httpx error may hold the request URL (token)
            log.warning("telegram sendMessage failed: %s", type(exc).__name__)
        return False

    async def alert(self, alert: Alert) -> bool:
        """Send unless the key is still cooling down (or was a one-off already sent). Failure is retried next tick."""
        now = self.clock()
        last = self._sent.get(alert.key)
        if last is not None and (alert.once or now - last < timedelta(minutes=self.settings.cooldown_minutes)):
            return False
        if not await self.send(alert.text):
            return False
        self._sent[alert.key] = now
        log.info("alert sent: %s", alert.key)
        return True

    # -- checks ---------------------------------------------------------------------------------------------------
    async def container_alerts(self) -> list[Alert]:
        if self.docker is None or not self.settings.docker_socket:
            return []
        try:
            response = await self.docker.get(
                "/containers/json",
                params={"all": "true", "filters": json.dumps({"label": [f"com.docker.compose.project={COMPOSE_PROJECT}"]})})
            response.raise_for_status()
            return container_alerts(container_problems(response.json()))
        except Exception as exc:  # noqa: BLE001
            log.warning("docker api check failed: %s", type(exc).__name__)
            return []

    async def database_alerts(self) -> list[Alert]:
        if self.store is None:
            return []
        now = self.clock()
        alerts: list[Alert] = []
        try:
            for campaign in await self.store.apify_error_streaks(now - timedelta(hours=6)):
                alerts.append(Alert(f"apify:{campaign}", f"Кампания {campaign[:8]}: {APIFY_ERRORS_IN_A_ROW} ошибки Apify "
                                    "подряд. Проверь токен/лимиты Apify и логи campaign-runner."))
            for campaign, spent in await self.store.over_budget(self.settings.budget_usd):
                alerts.append(Alert(f"budget:{campaign}", f"Кампания {campaign[:8]}: бюджет исчерпан, потрачено "
                                    f"${spent:.2f} из ${self.settings.budget_usd:.2f} (CAMPAIGN_BUDGET_USD)."))
            empty = await self.store.empty_campaign_streak()
            if empty:
                alerts.append(Alert(f"empty:{empty[0]}", f"{EMPTY_CAMPAIGNS_IN_A_ROW} завершённые кампании подряд без "
                                    "единой карточки. Проверь источники и фильтры.", once=True))
            for campaign, portal in await self.store.empty_portals(now - timedelta(days=1)):
                alerts.append(Alert(f"portal:{campaign}:{portal}", f"Портал {portal} (кампания {campaign[:8]}) "
                                    "вернул 0 объявлений.", once=True))
        except Exception as exc:  # noqa: BLE001
            log.warning("database check failed: %s", type(exc).__name__)
        return alerts

    async def daily_summary(self) -> bool:
        now = self.clock()
        today = now.date().isoformat()
        if self.store is None or now.hour != self.settings.summary_hour or self._summary_day == today:
            return False
        try:
            s = await self.store.summary(now - timedelta(days=1))
        except Exception as exc:  # noqa: BLE001
            log.warning("summary query failed: %s", type(exc).__name__)
            return False
        text = (f"Сводка за сутки: кампаний запущено {s.campaigns_started}, завершено {s.campaigns_finished}; "
                f"карточек {s.cards}; лидов {s.leads}; потрачено ${s.cost_usd:.2f}; ошибок {s.errors}.")
        if not await self.send(text):
            return False
        self._summary_day = today
        log.info("daily summary sent")
        return True

    async def tick(self) -> None:
        for alert in [*await self.container_alerts(), *await self.database_alerts()]:
            await self.alert(alert)
        await self.daily_summary()


async def serve(settings: WatchdogSettings, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    missing = settings.missing()
    if missing:
        log.info("watchdog idle (not set: %s)", ", ".join(missing))
        while True:
            await sleep(3600)
    import asyncpg

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    docker = (httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=settings.docker_socket),
                                base_url="http://docker", timeout=10) if settings.docker_socket else None)
    log.info("watchdog started: poll %ss, cooldown %s min, summary at %02d:00 UTC, containers %s",
             settings.poll_seconds, settings.cooldown_minutes, settings.summary_hour,
             "checked" if docker else "not checked")
    async with httpx.AsyncClient(timeout=15) as telegram:
        watchdog = Watchdog(settings, PostgresStore(pool), telegram, docker)
        try:
            while True:
                try:
                    await watchdog.tick()
                except Exception as exc:  # noqa: BLE001
                    log.warning("watchdog tick failed: %s", type(exc).__name__)
                await sleep(settings.poll_seconds)
        finally:
            if docker:
                await docker.aclose()
            await pool.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mute_http_logs()
    asyncio.run(serve(WatchdogSettings()))


if __name__ == "__main__":
    main()
