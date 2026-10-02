"""End-to-end system flows on a real PostgreSQL with every migration applied.

Only the outside world is replaced: Facebook pages (a fake browser and
reader), OpenRouter (a deterministic analyzer and transcriber) and Telegram
(recorded messages). Everything in between is the production code: the
control plane and its confirmations, the Orchestra dispatcher, the launch
queue and facebook-runner, the collector store, the Scrapling and Agent Reach
workers, the analysis worker and its digests, and the verification service.

Skipped unless SYSTEM_TEST_DATABASE_URL (or VERIFICATION_TEST_DATABASE_URL)
names a disposable database whose name ends in ``_test``; every test drops
and recreates its ``public`` schema.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
from pathlib import Path

import httpx
import pytest

from bot.acquisition.models import NormalizedPage
from bot.agent_reach.runner import ControlledAgentReach
from bot.agent_reach.store import PostgresReachStore
from bot.agent_reach.worker import step as reach_step
from bot.analysis_pipeline.main import run_once as analyse
from bot.analysis_pipeline.models import AnalysisResult
from bot.analysis_pipeline.pipeline import AnalysisPipeline
from bot.analysis_pipeline.settings import AnalysisSettings
from bot.analysis_pipeline.store import PostgresAnalysisStore
from bot.campaign.architect import plan_campaign
from bot.campaign.discovery import FacebookDiscovery, PostgresDiscoveryStore, plan_seeds
from bot.campaign.runner import VERIFY, CampaignRunner, RunnerConfig
from bot.campaign.runs import PostgresRunStore
from bot.campaign.store import PostgresCampaignStore
from bot.control_plane.access import AccessDesk, PostgresAccessStore
from bot.control_plane.auto import PostgresSettingsStore
from bot.control_plane.intake import PostgresIntakeStore
from bot.control_plane.models import IncomingMessage, Reply, TranscriptResult
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import PostgresControlPlaneStore
from bot.facebook_collector.browser import BrowserLease
from bot.facebook_collector.collector import FacebookBatchCollector
from bot.facebook_collector.models import ChallengeDetected, CollectedPost, GroupRead, GroupState
from bot.facebook_collector.runner import CollectorRunner, PostgresLaunchStore
from bot.facebook_collector.store import PostgresCollectorStore
from bot.operators import OperatorSet
from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.orchestra.models import ConfirmedCommand
from bot.orchestra.store import PostgresOrchestraStore, SafetyLimits
from bot.scrapling_connector.models import ScraplingOutcome, ScraplingResult
from bot.scrapling_connector.store import PostgresScraplingStore
from bot.scrapling_connector.worker import step as scrapling_step
from bot.verification.models import Recovery
from bot.verification.service import FlowConfig, VerificationService
from bot.verification.store import PostgresVerificationStore
from tests.test_campaign_discovery import NOW, FakeBrowser, FakeReader, Sleeps, link
from tests.test_campaign_runner import FakeMessenger
from tests.test_live_view import TOKEN, init_data
from tests.test_verification_flow import (
    OPERATOR,
    OWNER,
    FakeLive,
    FakeNotifier,
    FakeWatchdog,
    token_of,
)

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL") or os.environ.get("VERIFICATION_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
    reason="set SYSTEM_TEST_DATABASE_URL to a disposable *_test database",
)
MESSAGE_IDS = itertools.count(1)  # Telegram message ids are unique per chat across the whole run
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))

RENT_GROUP = "https://www.facebook.com/groups/madrid-pisos"
INVEST_GROUP = "https://www.facebook.com/groups/inversores-madrid"
RENT_POST = CollectedPost("fb-rent-1", "https://www.facebook.com/groups/madrid-pisos/posts/1",
                          "Piso en alquiler en Madrid centro, dos habitaciones, 1200 EUR al mes, disponible ya.",
                          "2026-09-20T10:00:00Z")
INVEST_POST = CollectedPost("fb-invest-1", "https://www.facebook.com/groups/inversores-madrid/posts/7",
                            "Buscamos inversores para una startup proptech en Madrid, ronda de capital de 200k.")
NOISE_POST = CollectedPost("fb-noise-1", "https://www.facebook.com/groups/madrid-pisos/posts/2", "Hola a todos")


@pytest.fixture
async def pool():
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.execute(
        "insert into browser_profiles(profile_name, platform, storage_locator, state) values('facebook-main','facebook','volume:fb','ready')")
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=8)
    try:
        yield pool
    finally:
        await pool.close()


# --- the outside world ------------------------------------------------------------


class FakeFacebook:
    """Browser plus group reader: each group returns its posts, or a challenge once."""

    def __init__(self, pages: dict[str, list[CollectedPost]], challenge_once: set[str] = frozenset()) -> None:
        self.pages, self.challenge_once = pages, set(challenge_once)
        self.reads: list[str] = []
        self.released: list[str] = []

    async def acquire(self, profile_id: str, profile_name: str, state: str, *, platform: str = "facebook") -> BrowserLease:
        return BrowserLease(profile_id, "lease")

    async def release(self, lease: BrowserLease, next_state: str = "READY") -> None:
        self.released.append(next_state)

    async def read(self, lease: BrowserLease, url: str) -> GroupRead:
        self.reads.append(url)
        if url in self.challenge_once:
            self.challenge_once.discard(url)
            raise ChallengeDetected("facebook_url:/checkpoint", {"url": "https://www.facebook.com/checkpoint/"})
        posts = tuple(self.pages.get(url, ()))
        return GroupRead(GroupState.ACTIVE, posts, {}, {"articles": len(posts)})


async def _no_sleep(_seconds: float) -> None:
    return None


def facebook_runner(pool, fb: FakeFacebook) -> CollectorRunner:
    async def run(batch_id: str) -> str:
        collector = FacebookBatchCollector(
            PostgresCollectorStore(pool), fb, fb, max_posts=5, item_timeout_seconds=30,
            pause_min_seconds=0, pause_max_seconds=0, sleep=_no_sleep,
        )
        return await collector.run(batch_id)

    return CollectorRunner(PostgresLaunchStore(pool), run)


class FakeAnalyzer:
    """OpenRouter stand-in: relevant when the text is about its own vertical."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def analyze(self, evidence, vertical: str) -> AnalysisResult:
        self.calls.append((evidence.canonical_url, vertical))
        text = evidence.text.lower()
        relevant = ("piso" in text) if vertical == "real_estate" else ("inversores" in text)
        return AnalysisResult(
            relevant=relevant, confidence=0.9, summary=f"{vertical} summary for {evidence.canonical_url}",
            location="Madrid" if vertical == "real_estate" else None,
            price_signals=["1200 EUR/month"] if vertical == "real_estate" else [],
            related_links=[], category=vertical if relevant else "other", reason="test",
        )


class Telegram:
    def __init__(self, fail: int = 0) -> None:
        self.messages: list[tuple[int, str]] = []
        self.fail = fail

    async def send(self, chat_id: int, body: str) -> int:
        if self.fail:
            self.fail -= 1
            raise httpx.ConnectError("telegram unreachable")
        self.messages.append((chat_id, body))
        return len(self.messages)


class FakeTranscriber:
    model, provider = "openai/whisper-large-v3-turbo", "openrouter"

    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
        return TranscriptResult(self.text, "en", 0.95, self.model)


def analysis_settings() -> AnalysisSettings:
    return AnalysisSettings(DATABASE_URL=URL, OPENROUTER_API_KEY="test-key", TELEGRAM_TOKEN=TOKEN, ANALYSIS_TELEGRAM_CHAT_ID=OWNER)


# --- the system ------------------------------------------------------------------


class System:
    """Control plane -> confirmation -> Orchestra, as wired in bot/control_plane/main.py."""

    def __init__(self, limits: SafetyLimits | None = None, transcriber: FakeTranscriber | None = None, auto_mode: bool = False) -> None:
        self.limits, self.transcriber, self.auto_mode = limits, transcriber, auto_mode
        self.notices: list[tuple[int, str]] = []

    async def __aenter__(self) -> System:
        self.orchestra = PostgresOrchestraStore(URL, self.limits)
        await self.orchestra.connect()
        self.dispatcher = OrchestraDispatcher(self.orchestra, operator_ids=frozenset({OWNER, OPERATOR}), notifier=self._notify,
                                              campaigns=PostgresCampaignStore(self.orchestra.pool))
        self.store = PostgresControlPlaneStore(URL)
        await self.store.connect()
        settings = ControlPlaneSettings(telegram_token=TOKEN, database_url=URL, operator_user_ids=frozenset({OWNER, OPERATOR}),
                                        auto_mode=self.auto_mode)
        self.control = ControlPlane(settings, self.store, self.transcriber, self._sink, settings_store=PostgresSettingsStore(self.store))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.store.close()
        await self.orchestra.close()

    async def _notify(self, chat_id: int, text: str) -> None:
        self.notices.append((chat_id, text))

    async def _sink(self, envelope):
        return await self.dispatcher.enqueue(ConfirmedCommand(
            envelope.command, envelope.arguments, envelope.chat_id, envelope.user_id, envelope.message_id, envelope.confirmation_id, envelope.auto))

    def _message(self, user: int, text: str | None = None, voice: bool = False) -> IncomingMessage:
        return IncomingMessage(chat_id=user, user_id=user, message_id=next(MESSAGE_IDS), text=text,
                               voice_file_id="voice-1" if voice else None, voice_size=2000 if voice else None,
                               voice_duration_seconds=3 if voice else None)

    async def confirm(self, reply: str, user: int) -> str:
        token = re.search(r"confirm (\S+)", reply)
        assert token, reply
        confirmed = (await self.control.handle_text(self._message(user, f"confirm {token.group(1)}"))).text
        assert confirmed.startswith("Confirmed"), confirmed
        assert await self.dispatcher.process_once()
        return self.notices[-1][1]

    async def command(self, text: str, user: int = OPERATOR) -> str:
        """Type a command, confirm it, let the dispatcher handle it; returns the Orchestra's answer."""
        return await self.confirm((await self.control.handle_text(self._message(user, text))).text, user)

    async def voice(self, user: int = OPERATOR) -> str:
        async def download() -> bytes:
            return b"OggS-voice"

        return (await self.control.handle_voice(self._message(user, voice=True), download)).text


def verification(watchdog: FakeWatchdog | None = None) -> tuple[VerificationService, FakeNotifier, PostgresVerificationStore]:
    store = PostgresVerificationStore(URL)
    notifier = FakeNotifier()
    service = VerificationService(
        store, FakeLive(), watchdog or FakeWatchdog(Recovery(True)), notifier,
        FlowConfig(public_url="https://1-2-3-4.sslip.io", operator_ids=frozenset({OWNER, OPERATOR}), owner_id=OWNER, bot_token=TOKEN),
    )
    return service, notifier, store


async def solve_and_resume(service: VerificationService, notifier: FakeNotifier) -> None:
    await service.tick()
    session = (await service.open(token_of(notifier.links(OPERATOR)[-1]), init_data(OPERATOR))).session
    await service.claim(session)
    await service.view(session)
    assert await service.solve(session) is True
    await service.resume(session)


# --- flows --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_command_to_sequential_batch_to_analysis_to_one_digest_per_vertical(pool) -> None:
    fb = FakeFacebook({RENT_GROUP: [RENT_POST, NOISE_POST], INVEST_GROUP: [INVEST_POST]})
    async with System() as system:
        answer = await system.command(f"/run facebook-groups {RENT_GROUP} {INVEST_GROUP}")
        assert "queued Facebook batch" in answer and "starts automatically" in answer
        batch = await pool.fetchval("select id::text from acquisition_batches")
        assert tuple(await pool.fetchrow("select state, notify_telegram_id from collector_launch_requests")) == ("pending", OPERATOR)

        runner = facebook_runner(pool, fb)
        assert await runner.step() is True
        assert fb.reads == [RENT_GROUP, INVEST_GROUP]  # in order, one browser lease
        assert fb.released == ["READY"]
        assert await pool.fetchval("select state from acquisition_batches where id=$1::uuid", batch) == "succeeded"
        assert await pool.fetchval("select state from browser_profiles") == "ready"
        assert await pool.fetchval("select count(*) from collected_posts where state='normalised'") == 3
        assert await pool.fetchval("select published_at is not null from collected_posts where platform_post_id='fb-rent-1'")

        analyzer, telegram = FakeAnalyzer(), Telegram()
        result = await analyse(None, AnalysisPipeline(analyzer), telegram.send, analysis_settings())
        assert len(result["findings"]) == 2
        # Deterministic filters keep irrelevant posts and verticals away from the model.
        assert sorted(analyzer.calls) == sorted([(RENT_POST.canonical_url, "real_estate"), (INVEST_POST.canonical_url, "investors")])
        bodies = dict((b.split("\n")[0], b) for _, b in telegram.messages)
        assert set(bodies) == {"🏠 Недвижимость", "📈 Инвестиции"}
        estate = bodies["🏠 Недвижимость"]
        assert "Локация: Madrid" in estate and "Цена: 1200 EUR/month" in estate and f"Ссылка: {RENT_POST.canonical_url}" in estate
        assert f"Ссылка: {INVEST_POST.canonical_url}" in bodies["📈 Инвестиции"]
        assert estate.endswith("Язык оригинала: испанский") and RENT_POST.body_text not in estate
        assert {c for c, _ in telegram.messages} == {OWNER}
        states = dict(await pool.fetch("select platform_post_id, state from collected_posts"))
        assert states == {"fb-rent-1": "analysed", "fb-invest-1": "analysed", "fb-noise-1": "rejected"}
        assert await pool.fetchval("select count(*) from findings where state='delivered'") == 2

        # Idempotency: nothing is analysed, sent or started twice.
        again = await analyse(None, AnalysisPipeline(analyzer), telegram.send, analysis_settings())
        assert again["findings"] == [] and len(telegram.messages) == 2 and len(analyzer.calls) == 2
        assert await runner.step() is False
        original = await pool.fetchrow("select arguments, telegram_message_id from orchestration_commands")
        receipt = await system.dispatcher.enqueue(
            ConfirmedCommand("run", original["arguments"], OPERATOR, OPERATOR, original["telegram_message_id"]))
        assert receipt.duplicate is True
        assert await pool.fetchval("select count(*) from acquisition_batches") == 1

        # The requester hears how the automatic run ended.
        service, notifier, store = verification()
        await store.connect()
        try:
            await service.tick()
            assert (OPERATOR, f"Batch {batch} finished: all its groups were read.") in [(c, t) for c, t, _ in notifier.sent]
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_voice_command_is_transcribed_confirmed_and_applied(pool) -> None:
    source = await pool.fetchval(
        """insert into monitoring_sources(platform, source_kind, vertical, canonical_url, acquisition_method, state)
           values('website','website','both','https://example.org/listings','scrapling','active') returning id::text""")
    async with System(transcriber=FakeTranscriber("Pause everything.")) as system:
        reply = await system.voice()
        assert "Understood as: /pause all" in reply and "Confirmation required for /pause" in reply
        assert await pool.fetchval("select state from monitoring_sources where id=$1::uuid", source) == "active"  # not before confirm
        answer = await system.confirm(reply, OPERATOR)
        assert "paused (1 affected)" in answer
        assert await pool.fetchval("select state from monitoring_sources where id=$1::uuid", source) == "paused"
        actors = {r[0] for r in await pool.fetch("select actor from orchestration_audit_log where entity_type='monitoring_sources'")}
        assert f"telegram:{OPERATOR}" in actors
        # A voice note from someone without control never reaches the provider or the Orchestra.
        assert "после одобрения доступа" in await system.voice(user=777)
        assert await pool.fetchval("select count(*) from orchestration_commands") == 1


@pytest.mark.asyncio
async def test_challenge_stops_the_batch_then_mini_app_solve_resumes_it_to_a_digest(pool) -> None:
    fb = FakeFacebook({RENT_GROUP: [RENT_POST], INVEST_GROUP: [INVEST_POST]}, challenge_once={INVEST_GROUP})
    async with System() as system:
        await system.command(f"/run facebook-groups {RENT_GROUP} {INVEST_GROUP}")
    batch = await pool.fetchval("select id::text from acquisition_batches")
    runner = facebook_runner(pool, fb)
    assert await runner.step() is True
    assert await pool.fetchval("select state from acquisition_batches") == "human_verification_required"
    assert await pool.fetchval("select state from browser_profiles") == "human_verification_required"
    assert tuple(await pool.fetchrow("select state, result from collector_launch_requests")) == ("finished", "human_verification_required")
    assert fb.released == ["VERIFICATION_REQUIRED"]
    # The group read before the challenge is kept; the challenged one is not.
    assert await pool.fetchval("select count(*) from collected_posts") == 1

    service, notifier, store = verification()
    await store.connect()
    try:
        await solve_and_resume(service, notifier)
        assert await pool.fetchval("select state from acquisition_batches") == "queued"
        assert await pool.fetchval("select count(*) from collector_launch_requests where state='pending'") == 1

        assert await runner.step() is True
        assert fb.reads == [RENT_GROUP, INVEST_GROUP, INVEST_GROUP]  # resumed from the challenged group only
        assert await pool.fetchval("select state from acquisition_batches") == "succeeded"
        assert await pool.fetchval("select state from browser_profiles") == "ready"
        await service.tick()
        texts = [t for c, t, _ in notifier.sent if c == OPERATOR]
        assert f"Batch {batch} stopped at a new challenge; a verification notice follows." in texts
        assert f"Batch {batch} finished: all its groups were read." in texts
        events = [r[0] for r in await pool.fetch("select event from verification_events order by id")]
        assert events[:1] == ["detected"] and "recovery_confirmed" in events and "resume" in events
    finally:
        await store.close()

    telegram = Telegram()
    result = await analyse(None, AnalysisPipeline(FakeAnalyzer()), telegram.send, analysis_settings())
    assert len(result["findings"]) == 2 and len(telegram.messages) == 2


@pytest.mark.asyncio
async def test_website_and_instagram_commands_reach_their_own_workers(pool) -> None:
    await pool.execute(
        "insert into browser_profiles(profile_name, platform, storage_locator, state) values('instagram-main','instagram','volume:ig','ready')")
    async with System() as system:
        assert "starts automatically" in await system.command("/run website https://example.org/listing/42")
        assert "starts automatically" in await system.command("/run instagram https://www.instagram.com/inversores.madrid")
    methods = dict(await pool.fetch("select acquisition_method, state from acquisition_runs"))
    assert methods == {"scrapling": "queued", "agent_ridge": "queued"}

    class Website:
        async def fetch(self, task):
            page = NormalizedPage(task.target, "Listing 42", "Piso en alquiler en Madrid, Salamanca, 1500 EUR al mes con terraza.", "website")
            return ScraplingResult(task.run_id, ScraplingOutcome.COMPLETED, page)

    scrapling = PostgresScraplingStore(URL)
    await scrapling.connect()
    try:
        assert await scrapling_step(scrapling, Website()) is True
        assert await scrapling_step(scrapling, Website()) is False
    finally:
        await scrapling.close()

    class Instagram:
        def __init__(self) -> None:
            self.visited: list[tuple[str, str]] = []

        async def acquire(self, profile_id, name, state, *, platform="facebook"):
            self.visited.append((platform, name))
            return BrowserLease(profile_id, "lease")

        async def snapshot(self, lease, url, timeout_ms):
            return {"url": url, "title": "inversores.madrid", "text": "Buscamos inversores para startup inmobiliaria, capital semilla."}

        async def release(self, lease, next_state="READY"):
            return None

        async def health(self):
            return True

    browser = Instagram()
    reach = PostgresReachStore(pool)
    assert await reach_step(reach, ControlledAgentReach(browser)) is True
    assert await reach_step(reach, ControlledAgentReach(browser)) is False
    assert browser.visited == [("instagram", "instagram-main")]
    assert dict(await pool.fetch("select acquisition_method, state from acquisition_runs")) == {"scrapling": "succeeded", "agent_ridge": "succeeded"}
    assert await pool.fetchval("select state from browser_profiles where platform='instagram'") == "ready"

    telegram = Telegram()
    result = await analyse(None, AnalysisPipeline(FakeAnalyzer()), telegram.send, analysis_settings())
    assert len(result["findings"]) == 2
    assert {b.split("\n")[0] for _, b in telegram.messages} == {"📈 Инвестиции", "🏠 Недвижимость"}


@pytest.mark.asyncio
async def test_agent_reach_challenge_goes_through_verification_and_the_run_is_retried(pool) -> None:
    await pool.execute(
        "insert into browser_profiles(profile_name, platform, storage_locator, state) values('tiktok-main','tiktok','volume:tt','ready')")
    async with System() as system:
        await system.command("/run tiktok https://www.tiktok.com/@pisos.madrid")

    class TikTok:
        def __init__(self) -> None:
            self.challenge = True

        async def acquire(self, profile_id, name, state, *, platform="facebook"):
            return BrowserLease(profile_id, "lease")

        async def snapshot(self, lease, url, timeout_ms):
            if self.challenge:
                self.challenge = False
                return {"url": url, "title": "Security check", "text": "Please complete the captcha"}
            return {"url": url, "title": "pisos.madrid", "text": "Piso en alquiler en Madrid, Retiro, 1100 EUR al mes."}

        async def release(self, lease, next_state="READY"):
            return None

        async def health(self):
            return True

    browser, reach = TikTok(), PostgresReachStore(pool)
    await reach_step(reach, ControlledAgentReach(browser))
    assert await pool.fetchval("select state from acquisition_runs") == "awaiting_human_verification"
    assert await pool.fetchval("select state from browser_profiles where platform='tiktok'") == "human_verification_required"
    assert await pool.fetchval("select job_type from verification_jobs") == "login"

    service, notifier, store = verification()
    await store.connect()
    try:
        await solve_and_resume(service, notifier)
    finally:
        await store.close()
    assert await pool.fetchval("select state from acquisition_runs") == "queued"
    assert await reach_step(reach, ControlledAgentReach(browser)) is True
    assert await pool.fetchval("select state from acquisition_runs") == "succeeded"
    assert await pool.fetchval("select count(*) from collected_posts where state='normalised'") == 1


@pytest.mark.asyncio
async def test_quotas_and_circuit_breakers_refuse_excess_work_before_it_exists(pool) -> None:
    limits = SafetyLimits(facebook_batches_per_day=1, facebook_groups_per_day=3, runs_per_day=2,
                          breaker_failures=2, breaker_challenges=1, breaker_window_hours=6)
    async with System(limits) as system:
        assert "queued Facebook batch" in await system.command(f"/run facebook-groups {RENT_GROUP} {INVEST_GROUP}")
        refused = await system.command("/run facebook-groups https://www.facebook.com/groups/third")
        assert "cannot run: daily quota reached: 1 of 1 Facebook batches" in refused
        assert await pool.fetchval("select count(*) from acquisition_batches") == 1

        for n in (1, 2):
            assert "queued run" in await system.command(f"/run website https://example.org/{n}")
        assert "daily quota reached: 2 of 2 scrapling runs" in await system.command("/run website https://example.org/3")
        assert await pool.fetchval("select count(*) from acquisition_runs") == 2
        failed = await pool.fetch("select error_code from orchestration_commands where state='failed'")
        assert [r[0] for r in failed] == ["precondition_failed", "precondition_failed"]

    # Breakers: repeated failures of a method, and challenges on a platform.
    await pool.execute("update acquisition_runs set state='running' where acquisition_method='scrapling'")
    await pool.execute("update acquisition_runs set state='failed', finished_at=now() where acquisition_method='scrapling'")
    async with System(SafetyLimits(runs_per_day=10, breaker_failures=2, breaker_challenges=1)) as system:
        answer = await system.command("/run website https://example.org/4")
        assert "safety breaker open: 2 failed scrapling runs" in answer
        source = await pool.fetchval("select id from monitoring_sources where platform='facebook' limit 1")
        await pool.execute("insert into verification_jobs(source_id, job_type, requested_by) values($1,'facebook_challenge','test')", source)
        answer = await system.command(f"/run facebook-groups {RENT_GROUP}")
        assert "safety breaker open: 1 facebook challenges" in answer
    assert await pool.fetchval("select count(*) from acquisition_runs") == 2
    assert await pool.fetchval("select count(*) from acquisition_batches") == 1


@pytest.mark.asyncio
async def test_auto_mode_switch_is_persisted_and_auto_queued_work_still_meets_quotas(pool) -> None:
    limits = SafetyLimits(facebook_batches_per_day=1, runs_per_day=1)
    async with System(limits) as system:
        assert "off (AUTO_MODE default" in (await system.control.handle_text(system._message(OWNER, "/auto status"))).text
        assert (await system.control.handle_text(system._message(OWNER, "/auto on"))).text.startswith("Auto mode is on")
    assert tuple(await pool.fetchrow("select value #>> '{}', updated_by from control_settings where key='auto_mode'")) == ("on", OWNER)
    audit = await pool.fetchrow("select actor, action, new_state from orchestration_audit_log where entity_type='control_settings'")
    assert tuple(audit) == (f"telegram:{OWNER}", "insert", "on")

    async with System(limits) as system:  # a restart keeps the switch; .env says off
        for n in (1, 2):
            reply = (await system.control.handle_text(system._message(OPERATOR, f"/run website https://example.org/{n}"))).text
            assert reply.startswith("Авто: /run поставлена в очередь"), reply
            assert await system.dispatcher.process_once()
        assert "queued run" in system.notices[-3][1]
        assert "daily quota reached: 1 of 1 scrapling runs" in system.notices[-1][1]
        assert await pool.fetchval("select count(*) from acquisition_runs") == 1
        rows = await pool.fetch("select confirmation_id, state, error_code from orchestration_commands order by created_at")
        assert [tuple(r) for r in rows] == [(None, "finished", None), (None, "failed", "precondition_failed")]
        actors = await pool.fetch("select actor from orchestration_audit_log where entity_type='orchestration_commands' and action='insert'")
        assert {r[0] for r in actors} == {f"telegram:{OPERATOR}:auto"}

        # A goal written as plain text becomes a planned campaign; /cancel still asks.
        reply = (await system.control.handle_text(system._message(OPERATOR, "квартиры в аренду в Мадриде"))).text
        assert reply.startswith("Авто: /campaign")
        assert await system.dispatcher.process_once()
        assert "запланирована" in system.notices[-1][1]
        assert await pool.fetchval("select count(*) from campaigns where requested_by=$1", OPERATOR) == 1
        assert "Confirmation required" in (await system.control.handle_text(system._message(OPERATOR, "/cancel all"))).text

        assert (await system.control.handle_text(system._message(OWNER, "/auto off"))).text.startswith("Auto mode is off")
        assert "Confirmation required" in (await system.control.handle_text(system._message(OPERATOR, "/pause all"))).text
    assert await pool.fetchval("select count(*) from orchestration_audit_log where entity_type='control_settings'") == 2


@pytest.mark.asyncio
async def test_two_analysis_workers_never_share_a_post_and_a_lost_claim_is_taken_over(pool) -> None:
    source = await pool.fetchval(
        """insert into monitoring_sources(platform, source_kind, vertical, canonical_url, acquisition_method, state)
           values('website','website','both','https://example.org/feed','scrapling','active') returning id""")
    for n in range(3):
        await pool.execute(
            """insert into collected_posts(source_id, platform_post_id, canonical_url, body_text, content_hash, state)
               values($1, $2, $3, 'Piso en alquiler en Madrid, 1200 EUR al mes, dos habitaciones.', $2, 'normalised')""",
            source, f"p{n}", f"https://example.org/feed/{n}")
    first, second = PostgresAnalysisStore(URL), PostgresAnalysisStore(URL)
    await first.connect()
    await second.connect()
    try:
        a, b = await asyncio.gather(first.pending(2), second.pending(2))
        ids_a, ids_b = {r["id"] for r in a}, {r["id"] for r in b}
        assert not ids_a & ids_b and len(ids_a | ids_b) == 3

        # A worker that died keeps its claim only until the claim time passes.
        lost = a[0]
        await pool.execute("update collected_posts set analysis_claimed_at = now() - interval '10 minutes' where id=$1", lost["id"])
        taken = await second.pending(5, claim_seconds=300)
        assert [r["id"] for r in taken] == [lost["id"]]
        assert await first.finalize(str(lost["id"]), str(lost["analysis_claim_token"]), accepted=True) is False
        assert await second.finalize(str(lost["id"]), str(taken[0]["analysis_claim_token"]), accepted=True) is True
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_a_digest_telegram_refused_is_sent_later_exactly_once(pool) -> None:
    source = await pool.fetchval(
        """insert into monitoring_sources(platform, source_kind, vertical, canonical_url, acquisition_method, state)
           values('website','website','real_estate','https://example.org/r','scrapling','active') returning id""")
    await pool.execute(
        """insert into collected_posts(source_id, platform_post_id, canonical_url, body_text, content_hash, state)
           values($1, 'r1', 'https://example.org/r/1', 'Piso en alquiler en Madrid, Chamberí, 1300 EUR al mes.', 'r1', 'normalised')""",
        source)
    down = Telegram(fail=1)
    await analyse(None, AnalysisPipeline(FakeAnalyzer()), down.send, analysis_settings())
    assert down.messages == []
    assert await pool.fetchval("select state from analysis_digests") == "queued"
    assert await pool.fetchval("select state from findings") == "ready"

    await analyse(None, AnalysisPipeline(FakeAnalyzer()), down.send, analysis_settings())
    await analyse(None, AnalysisPipeline(FakeAnalyzer()), down.send, analysis_settings())
    assert len(down.messages) == 1
    assert await pool.fetchval("select state from analysis_digests") == "sent"
    assert await pool.fetchval("select state from findings") == "delivered"


# --- campaigns ----------------------------------------------------------------------------

CAMPAIGN_GOAL = "Найди квартиры в аренду в Мадриде"
PISOS = "https://www.facebook.com/groups/pisosmadrid/"
RENT = "https://www.facebook.com/groups/rentmadrid/"


class CampaignFacebook(FakeFacebook):
    """The group reader also lets the campaign runner look while a group is being read."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.during_read = None

    async def read(self, lease: BrowserLease, url: str) -> GroupRead:
        if self.during_read is not None:
            await self.during_read(url)
        return await super().read(lease, url)


def campaign_runner(pool, messenger: FakeMessenger) -> CampaignRunner:
    campaigns = PostgresCampaignStore(pool)
    plan = plan_campaign(CAMPAIGN_GOAL)
    seeds = plan_seeds(plan, 12)
    browser = FakeBrowser({seeds[0][1]: [link("pisosmadrid", "Pisos alquiler Madrid", "10 posts a day"),
                                         link("rentmadrid", "Madrid rent", "3 posts a week")]})
    discovery = FacebookDiscovery(campaigns, PostgresDiscoveryStore(pool), browser, FakeReader(), sleep=Sleeps(), now=lambda: NOW)
    return CampaignRunner(campaigns, PostgresRunStore(pool, SafetyLimits()), messenger, discovery,
                          config=RunnerConfig(window_cooldown_seconds=0, analysis_grace_seconds=600), owner_ids={OPERATOR})


async def start_campaign(pool) -> str:
    async with System() as system:
        answer = await system.command(f"/campaign {CAMPAIGN_GOAL}")
    campaign_id = await pool.fetchval("select id::text from campaigns")
    assert answer.startswith(f"Кампания {campaign_id} запланирована: ")
    row = await pool.fetchrow("select state, telegram_chat_id, requested_by, source_text from campaigns")
    assert tuple(row) == ("planned", OPERATOR, OPERATOR, CAMPAIGN_GOAL)
    return campaign_id


@pytest.mark.asyncio
async def test_campaign_from_command_to_streamed_finding_and_completion(pool) -> None:
    campaign_id = await start_campaign(pool)
    messenger = FakeMessenger()
    runner = campaign_runner(pool, messenger)

    await runner.tick()  # discovery, then window 1 as one ordinary batch for facebook-runner
    assert await pool.fetchval("select state from campaigns") == "running"
    batch = await pool.fetchrow("select id::text, campaign_id::text, max_items, state from acquisition_batches")
    assert (batch["campaign_id"], batch["max_items"], batch["state"]) == (campaign_id, 2, "queued")
    assert tuple(await pool.fetchrow("select state, notify_telegram_id from collector_launch_requests")) == ("pending", None)
    assert await pool.fetchval(
        "select count(*) from campaign_groups where batch_id=$1::uuid and state='queued'", batch["id"]) == 2
    assert await pool.fetchval("select status_message_id from campaigns") == 1

    fb = CampaignFacebook({PISOS: [RENT_POST], RENT: [NOISE_POST]})
    reading: list[str] = []

    async def look(url: str) -> None:
        if url == PISOS:
            await runner.tick()
            reading.append(messenger.edits[-1][2])

    fb.during_read = look
    assert await facebook_runner(pool, fb).step() is True
    assert fb.reads == [PISOS, RENT]
    assert reading == [f"🎯 {(await runner.campaigns.get(campaign_id)).plan.goal}\nСейчас: Facebook · Pisos alquiler Madrid · ищу дальше"]
    assert await pool.fetchval("select state from acquisition_batches") == "succeeded"

    digest = Telegram()
    result = await analyse(None, AnalysisPipeline(FakeAnalyzer()), digest.send, analysis_settings())
    assert len(result["findings"]) == 1
    assert digest.messages == [] and await pool.fetchval("select count(*) from analysis_digests") == 0

    await runner.tick()  # streams the finding, closes the window
    await runner.tick()  # nothing queued any more: completes
    findings = messenger.findings()
    assert len(findings) == 1 and RENT_POST.canonical_url in findings[0]
    assert findings[0].endswith("🔎 Найдено: 1 · ищу дальше")
    assert findings[0].startswith("🏠 Недвижимость") and "Location" not in findings[0]
    assert "Язык оригинала: испанский\n\n🔎" in findings[0] and RENT_POST.body_text not in findings[0]
    assert {chat for chat, _, _ in messenger.sent} == {OPERATOR}
    assert tuple(await pool.fetchrow("select state, stop_reason from campaigns")) == ("completed", "queue_exhausted")
    assert await pool.fetchval("select state from findings") == "delivered"
    assert dict(await pool.fetch("select group_key, state from campaign_groups")) == {"pisosmadrid": "collected", "rentmadrid": "collected"}
    assert tuple(await pool.fetchrow("select state, outcome from campaign_windows")) == ("finished", "succeeded")
    assert messenger.edits[-1][2].endswith("Кампания завершена · найдено 1")
    assert messenger.edits[-1][1] == await pool.fetchval("select status_message_id from campaigns")

    # Restarts and later cycles never send anything twice.
    edits, sent = len(messenger.edits), len(messenger.sent)
    await campaign_runner(pool, messenger).tick()
    await analyse(None, AnalysisPipeline(FakeAnalyzer()), digest.send, analysis_settings())
    assert (len(messenger.edits), len(messenger.sent), digest.messages) == (edits, sent, [])
    assert await pool.fetchval("select count(*) from campaign_findings where state='sent'") == 1
    actors = {r[0] for r in await pool.fetch(
        "select actor from orchestration_audit_log where entity_type='campaigns' and action='state_transition'")}
    assert actors == {"campaign:discovery", "campaign:runner"}


@pytest.mark.asyncio
async def test_campaign_window_challenge_pauses_until_verification_resumes_it(pool) -> None:
    await start_campaign(pool)
    messenger = FakeMessenger()
    runner = campaign_runner(pool, messenger)
    await runner.tick()

    fb = CampaignFacebook({PISOS: [RENT_POST], RENT: [NOISE_POST]}, challenge_once={RENT})
    collector = facebook_runner(pool, fb)
    assert await collector.step() is True
    assert await pool.fetchval("select state from acquisition_batches") == "human_verification_required"
    await runner.tick()
    assert await pool.fetchval("select state from campaigns") == "paused_verification"
    assert messenger.edits[-1][2].endswith(VERIFY)
    await runner.tick()
    assert await pool.fetchval("select count(*) from acquisition_batches") == 1  # nothing new while paused

    service, notifier, store = verification()
    await store.connect()
    try:
        await solve_and_resume(service, notifier)
    finally:
        await store.close()
    await runner.tick()
    assert await pool.fetchval("select state from campaigns") == "running"
    assert await collector.step() is True
    assert fb.reads == [PISOS, RENT, RENT]
    assert await pool.fetchval("select state from acquisition_batches") == "succeeded"

    await analyse(None, AnalysisPipeline(FakeAnalyzer()), Telegram().send, analysis_settings())
    await runner.tick()
    await runner.tick()
    assert await pool.fetchval("select state from campaigns") == "completed"
    assert len(messenger.findings()) == 1
    assert messenger.edits[-1][2].endswith("Кампания завершена · найдено 1")


@pytest.mark.asyncio
async def test_campaign_cancel_command_cancels_the_in_flight_window(pool) -> None:
    campaign_id = await start_campaign(pool)
    messenger = FakeMessenger()
    runner = campaign_runner(pool, messenger)
    await runner.tick()
    async with System() as system:
        status = await system.command(f"/campaign cancel {campaign_id}")
        assert status == f"Кампания {campaign_id} остановлена."
        reply = (await system.control.handle_text(system._message(OPERATOR, "/campaign status"))).text
        assert "Confirmation" not in reply
        assert await system.dispatcher.process_once()
        assert system.notices[-1][1].startswith(f"Кампания {campaign_id}: остановлена")
    await runner.tick()
    assert await pool.fetchval("select state from acquisition_batches") == "cancelled"
    assert await pool.fetchval("select state from campaign_windows") == "finished"
    assert messenger.edits[-1][2].endswith("Кампания остановлена")
    # facebook-runner skips the cancelled batch; the campaign never issues another window.
    assert await facebook_runner(pool, CampaignFacebook({})).step() is True
    assert await pool.fetchval("select state from collector_launch_requests") == "skipped"
    await runner.tick()
    assert await pool.fetchval("select count(*) from acquisition_batches") == 1


@pytest.mark.asyncio
async def test_a_discovery_challenge_counts_toward_the_facebook_breaker(pool) -> None:
    campaign_id = await start_campaign(pool)
    campaigns = PostgresCampaignStore(pool)
    await campaigns.set_state(campaign_id, "discovering", "campaign:discovery")
    await campaigns.set_state(campaign_id, "paused_verification", "campaign:discovery", reason="facebook_challenge:x")
    async with System(SafetyLimits(breaker_challenges=1)) as system:
        answer = await system.command(f"/run facebook-groups {RENT_GROUP}")
    assert "safety breaker open: 1 facebook challenges" in answer
    assert await pool.fetchval("select count(*) from acquisition_batches") == 0


@pytest.mark.asyncio
async def test_runner_startup_frees_a_profile_stuck_by_a_crashed_discovery_only_when_unheld(pool) -> None:
    campaign_id = await start_campaign(pool)
    await PostgresCampaignStore(pool).set_state(campaign_id, "discovering", "campaign:discovery")
    profile = await pool.fetchval("update browser_profiles set state='in_use' returning id")
    runner = campaign_runner(pool, FakeMessenger())

    # A running batch on the profile legitimately holds it.
    batch = await pool.fetchval(
        "insert into acquisition_batches(platform, acquisition_method, vertical, state) values('facebook','facebook_connector','both','queued') returning id")
    await pool.execute("update acquisition_batches set state='running' where id=$1", batch)
    batch_run = await pool.fetchval(
        "insert into batch_runs(batch_id, browser_profile_id, state) values($1,$2,'queued') returning id", batch, profile)
    await pool.execute("update batch_runs set state='running' where id=$1", batch_run)
    assert await runner.recover() == 0
    assert await pool.fetchval("select state from browser_profiles") == "in_use"

    await pool.execute("update batch_runs set state='succeeded', finished_at=now() where id=$1", batch_run)
    assert await runner.recover() == 1
    assert await pool.fetchval("select state from browser_profiles") == "ready"
    actor = await pool.fetchval(
        "select actor from orchestration_audit_log where entity_type='browser_profiles' order by id desc limit 1")
    assert actor == "campaign:runner:recovery"
    assert await pool.fetchval("select state from campaigns") == "discovering"
    await runner.tick()  # discovery resumes from its saved progress
    assert await pool.fetchval("select state from campaigns") == "running"


# --- the user role ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_user_gives_a_task_answers_a_question_and_launches_a_campaign(pool) -> None:
    """Owner approves as user -> /start -> mode -> task without a city -> answer -> Запустить -> campaign."""
    user = 777
    store = PostgresControlPlaneStore(URL)
    await store.connect()
    orchestra = PostgresOrchestraStore(URL)
    await orchestra.connect()
    inbox: list[tuple[int, Reply]] = []
    notices: list[tuple[int, str]] = []

    async def send(chat_id: int, reply: Reply) -> None:
        inbox.append((chat_id, reply))

    async def notify(chat_id: int, text: str) -> None:
        notices.append((chat_id, text))

    operators = OperatorSet({OWNER})
    access = AccessDesk(PostgresAccessStore(store), operators, notify=send)
    dispatcher = OrchestraDispatcher(orchestra, operator_ids=operators.controllers, notifier=notify,
                                     campaigns=PostgresCampaignStore(orchestra.pool), roles=operators)

    async def sink(envelope):
        return await dispatcher.enqueue(ConfirmedCommand(envelope.command, envelope.arguments, envelope.chat_id, envelope.user_id,
                                                         envelope.message_id, envelope.confirmation_id, envelope.auto))

    settings = ControlPlaneSettings(telegram_token=TOKEN, database_url=URL, operator_user_ids=frozenset({OWNER}))
    control = ControlPlane(settings, store, None, sink, access=access, settings_store=PostgresSettingsStore(store),
                           intake_store=PostgresIntakeStore(store))

    def message(text: str) -> IncomingMessage:
        return IncomingMessage(chat_id=user, user_id=user, message_id=next(MESSAGE_IDS), text=text)

    try:
        await control.handle_callback(user, "access:request", "Ann", "ann")
        [(_, request)] = [(c, r) for c, r in inbox if c == OWNER]
        approve_as_user = next(b.callback_data for b in request.buttons if b.callback_data.startswith("access:user:"))
        assert (await control.handle_callback(OWNER, approve_as_user)).text.startswith("Approved as user")
        assert tuple(await pool.fetchrow("select role, state from telegram_operators where telegram_user_id=$1", user)) == ("user", "approved")
        restarted = OperatorSet({OWNER})
        await AccessDesk(PostgresAccessStore(store), restarted).load()
        assert restarted.role(user) == "user" and user not in restarted

        start = await control.handle_text(message("/start"))
        assert [b.callback_data for b in start.buttons] == ["mode:real_estate", "mode:investors"]
        await control.handle_callback(user, "mode:real_estate", chat_id=user)
        assert await pool.fetchval("select mode from user_task_drafts where telegram_user_id=$1", user) == "real_estate"

        question = await control.handle_text(message("снять квартиру до 1000 €"))
        assert question.text.startswith("В каком городе искать?")
        assert await pool.fetchval("select step from user_task_drafts where telegram_user_id=$1", user) == "city"
        summary = await control.handle_text(message("Мадрид"))
        assert "Город: Мадрид" in summary.text and "Сделка: аренда" in summary.text
        assert await pool.fetchval("select count(*) from orchestration_commands") == 0  # nothing without Запустить

        assert (await control.handle_callback(user, "task:launch", chat_id=user)).text.startswith("Принято. Начинаю поиск.")
        assert "устарела" in (await control.handle_callback(user, "task:launch", chat_id=user)).text
        assert await pool.fetchval("select count(*) from orchestration_commands") == 1
        assert (await pool.fetchval("select arguments from orchestration_commands")).startswith("mode=real_estate city=Madrid ")
        assert await dispatcher.process_once()
        assert await pool.fetchval("select source_text from campaigns") == "снять квартиру до 1000 €"
        owner_notices = [r.text for c, r in inbox if c == OWNER and r.text.startswith("Пользователь ")]
        assert len(owner_notices) == 1 and owner_notices[0].startswith("Пользователь Unknown, ID 777 запустил кампанию: ")
        row = await pool.fetchrow("select requested_by, telegram_chat_id, plan->>'vertical', plan->>'location', plan->'constraints'->>'deal' from campaigns")
        assert tuple(row) == (user, user, "real_estate", "Madrid", "rent")
        # No queue or campaign ids for a user: the runner's status message carries the progress.
        assert [text for chat, text in notices if chat == user] == []
        draft = await pool.fetchrow("select step, draft::text, launched_at from user_task_drafts where telegram_user_id=$1", user)
        assert draft[0] == "idle" and draft[1] == "{}" and draft[2] is not None
        actors = await pool.fetch("select actor from orchestration_audit_log where entity_type='orchestration_commands' and action='insert'")
        assert {r[0] for r in actors} == {f"telegram:{user}"}

        # Investors mode is authoritative even when the task mentions flats.
        await control.handle_callback(user, "mode:investors", chat_id=user)
        assert "Проверьте задачу" in (await control.handle_text(message("инвесторы и квартиры в Барселоне"))).text
        await control.handle_callback(user, "task:launch", "Ann", "ann", chat_id=user)
        assert (await pool.fetchval("select arguments from orchestration_commands order by created_at desc limit 1")).startswith(
            "mode=investors city=Barcelona ")
        assert await dispatcher.process_once()
        row = await pool.fetchrow("select source_text, plan->>'vertical', plan->>'location' from campaigns where requested_by=$1 "
                                  "order by created_at desc limit 1", user)
        assert tuple(row) == ("инвесторы и квартиры в Барселоне", "investors", "Barcelona")
        owner_notices = [r.text for c, r in inbox if c == OWNER and r.text.startswith("Пользователь ")]
        assert len(owner_notices) == 2 and owner_notices[-1].startswith("Пользователь Ann (@ann), ID 777 запустил кампанию: investors")

        # Several cities: the typed one is stored, with the task text as written.
        await control.handle_callback(user, "mode:real_estate", chat_id=user)
        question = await control.handle_text(message("квартиры в аренду в Мадриде или Валенсии до 900 €"))
        assert "несколько городов" in question.text and question.buttons == ()
        assert "Город: Валенсия" in (await control.handle_text(message("Валенсия"))).text
        await control.handle_callback(user, "task:launch", chat_id=user)
        assert await dispatcher.process_once()
        row = await pool.fetchrow("select source_text, plan->>'vertical', plan->>'location' from campaigns where requested_by=$1 "
                                  "order by created_at desc limit 1", user)
        assert tuple(row) == ("квартиры в аренду в Мадриде или Валенсии до 900 €", "real_estate", "Valencia")
        assert await pool.fetchval("select count(*) from orchestration_commands") == 3

        # A day-old summary cannot be launched.
        assert "Проверьте задачу" in (await control.handle_text(message("квартиры в аренду в Севилье до 800 €"))).text
        await pool.execute("update user_task_drafts set updated_at = now() - interval '25 hours' where telegram_user_id=$1", user)
        stale = await control.handle_callback(user, "task:launch", "Ann", "ann", chat_id=user)
        assert stale.text == "Черновик устарел, опишите задачу заново."
        assert await pool.fetchval("select count(*) from orchestration_commands") == 3

        # Still no operator commands for a user; the Orchestra refuses them even if queued.
        assert "недоступна" in (await control.handle_text(message("/run website https://example.org"))).text
        await dispatcher.enqueue(ConfirmedCommand("pause", "all", user, user, next(MESSAGE_IDS)))
        assert await dispatcher.process_once()
        assert await pool.fetchval("select error_code from orchestration_commands where command='pause'") == "not_operator"
    finally:
        await orchestra.close()
        await store.close()


@pytest.mark.asyncio
async def test_agent_reach_cancels_a_platform_it_cannot_read_instead_of_blocking_the_queue(pool) -> None:
    await pool.execute(
        "insert into browser_profiles(profile_name, platform, storage_locator, state) values"
        " ('linkedin-main','linkedin','volume:li','ready'), ('tiktok-main','tiktok','volume:tt','ready')")
    linkedin = await pool.fetchval(
        """insert into monitoring_sources(platform, source_kind, vertical, canonical_url, acquisition_method, state)
           values ('linkedin', 'account', 'investors', 'https://www.linkedin.com/in/ana', 'agent_ridge', 'active')
           returning id""")
    tiktok = await pool.fetchval(
        """insert into monitoring_sources(platform, source_kind, vertical, canonical_url, acquisition_method, state)
           values ('tiktok', 'account', 'real_estate', 'https://www.tiktok.com/@pisos.madrid', 'agent_ridge', 'active')
           returning id""")
    for source, platform in ((linkedin, "linkedin"), (tiktok, "tiktok")):
        await pool.execute(
            """insert into acquisition_runs(source_id, browser_profile_id, acquisition_method)
               select $1, id, 'agent_ridge' from browser_profiles where platform = $2""", source, platform)

    class TikTok:
        def __init__(self) -> None:
            self.visited: list[str] = []

        async def acquire(self, profile_id, name, state, *, platform="facebook"):
            return BrowserLease(profile_id, "lease")

        async def snapshot(self, lease, url, timeout_ms):
            self.visited.append(url)
            return {"url": url, "title": "pisos.madrid", "text": "Piso en alquiler en Madrid, Retiro, 1100 EUR al mes."}

        async def release(self, lease, next_state="READY"):
            return None

        async def health(self):
            return True

    reach, browser = PostgresReachStore(pool), TikTok()
    assert await reach_step(reach, ControlledAgentReach(browser)) is True
    assert browser.visited == ["https://www.tiktok.com/@pisos.madrid"], "the LinkedIn run no longer holds the queue"
    rows = dict(await pool.fetch(
        """select s.platform, r.state || ':' || coalesce(r.error_code, '') from acquisition_runs r
             join monitoring_sources s on s.id = r.source_id"""))
    assert rows == {"linkedin": "cancelled:unsupported_platform", "tiktok": "succeeded:"}
