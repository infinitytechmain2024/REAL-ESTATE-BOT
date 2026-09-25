"""Human verification flow: tokens, access control, decisions, watchdog, audit."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bot.verification.browser import BrowserUnavailable, judge
from bot.verification.classify import classify
from bot.verification.models import Job, Recovery
from bot.verification.service import AccessDenied, ActionRefused, FlowConfig, VerificationService
from bot.verification.settings import VerificationSettings, https_origin
from bot.verification.store import MemoryVerificationStore
from tests.test_live_view import TOKEN, init_data

OWNER, OPERATOR, OTHER, OUTSIDER = 11, 12, 13, 99
PUBLIC = "https://1-2-3-4.sslip.io"


class FakeLive:
    def __init__(self, fail: bool = False) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.fail = fail

    async def start(self, profile_id: str, profile_name: str, platform: str, url: str, minutes: int) -> str:
        if self.fail:
            raise BrowserUnavailable("the browser is busy")
        self.started.append(f"{profile_id}@{url}")
        return "vncpass1"

    async def stop(self, profile_id: str) -> None:
        self.stopped.append(profile_id)


class FakeWatchdog:
    def __init__(self, *results: Recovery) -> None:
        self.results = list(results) or [Recovery(True)]
        self.calls = 0

    async def check(self, profile_id: str, profile_name: str, platform: str, url: str) -> Recovery:
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, tuple[str, str] | None]] = []

    async def send(self, chat_id: int, text: str, button: tuple[str, str] | None = None) -> None:
        if chat_id == OUTSIDER:
            raise RuntimeError("chat not found")
        self.sent.append((chat_id, text, button))

    def links(self, chat_id: int) -> list[str]:
        return [b[1] for c, _, b in self.sent if c == chat_id and b]


def new_job(note: str = "facebook_url:/checkpoint", **changes: object) -> Job:
    base = Job(
        id=str(uuid.uuid4()), state="requested", job_type="facebook_challenge", source_id=str(uuid.uuid4()),
        source_url="https://www.facebook.com/groups/plots", platform="facebook", resolution_note=note,
        profile_id=str(uuid.uuid4()), profile_name="facebook-main", profile_state="human_verification_required",
        batch_id=str(uuid.uuid4()),
    )
    return replace(base, **changes)


def flow(*recoveries: Recovery, live: FakeLive | None = None, operators: frozenset[int] = frozenset({OWNER, OPERATOR, OTHER})):
    store, notifier = MemoryVerificationStore(), FakeNotifier()
    watchdog = FakeWatchdog(*recoveries)
    service = VerificationService(
        store, live or FakeLive(), watchdog, notifier,
        FlowConfig(public_url=PUBLIC, operator_ids=operators, owner_id=OWNER, bot_token=TOKEN),
    )
    return service, store, notifier, watchdog


def token_of(link: str) -> str:
    return link.rsplit("/v/", 1)[1]


def events(store: MemoryVerificationStore, job: Job) -> list[str]:
    return [e.event for e in store.log.get(job.id, [])]


async def announced(service: VerificationService, store: MemoryVerificationStore, job: Job | None = None) -> Job:
    job = store.add_job(job or new_job())
    await service.tick()
    return store.jobs[job.id]


# --- announcement and tokens -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_job_sends_every_operator_a_single_use_tailnet_link() -> None:
    service, store, notifier, _ = flow()
    job = await announced(service, store)
    assert job.notified_at and job.expires_at and job.challenge_kind == "checkpoint"
    for user in (OWNER, OPERATOR, OTHER):
        [link] = notifier.links(user)
        assert link.startswith(f"{PUBLIC}/verify/v/")
        # Only a hash is stored, never the secret itself.
        assert token_of(link) not in store.tokens
    assert events(store, job) == ["detected"] + ["token_issued", "notified"] * 3
    await service.tick()
    assert len(notifier.sent) == 3  # announced once


@pytest.mark.asyncio
async def test_an_unreachable_operator_does_not_stop_the_rest() -> None:
    service, store, notifier, _ = flow(operators=frozenset({OWNER, OUTSIDER}))
    job = await announced(service, store)
    assert notifier.links(OWNER) and events(store, job).count("notified") == 1


@pytest.mark.asyncio
async def test_a_link_opens_once_for_the_operator_telegram_signed() -> None:
    service, store, notifier, _ = flow()
    job = await announced(service, store)
    token = token_of(notifier.links(OPERATOR)[0])
    opened = await service.open(token, init_data(OPERATOR))
    assert opened.session.job_id == job.id and opened.session.user_id == OPERATOR
    assert opened.session.identity == f"telegram:{OPERATOR}" and len(opened.cookie) == 43
    with pytest.raises(AccessDenied, match="already used"):
        await service.open(token, init_data(OPERATOR))
    assert "opened" in events(store, job)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signed", "match"),
    [
        (None, "button in the Telegram bot"),
        ("", "button in the Telegram bot"),
        ("user=%7B%22id%22%3A12%7D&auth_date=1&hash=00", "button in the Telegram bot"),  # forged
        ("forwarded-to-stranger", "Only operators"),
    ],
)
async def test_only_a_telegram_signed_operator_gets_in(signed: str | None, match: str) -> None:
    service, store, notifier, _ = flow()
    await announced(service, store)
    token = token_of(notifier.links(OPERATOR)[0])
    with pytest.raises(AccessDenied, match=match):
        await service.open(token, init_data(OUTSIDER) if signed == "forwarded-to-stranger" else signed)
    # None of these burned the link: its recipient still gets in.
    assert (await service.open(token, init_data(OPERATOR))).session.user_id == OPERATOR


@pytest.mark.asyncio
async def test_another_operator_cannot_use_or_burn_someone_elses_link() -> None:
    service, store, notifier, _ = flow()
    await announced(service, store)
    token = token_of(notifier.links(OPERATOR)[0])
    with pytest.raises(AccessDenied, match="sent to someone else"):
        await service.open(token, init_data(OTHER))
    assert (await service.open(token, init_data(OPERATOR))).session.user_id == OPERATOR


@pytest.mark.asyncio
async def test_expired_malformed_and_foreign_tokens_are_refused() -> None:
    service, store, notifier, _ = flow()
    job = await announced(service, store)
    for record in store.tokens.values():
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(AccessDenied, match="expired"):
        await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))
    with pytest.raises(AccessDenied, match="invalid"):
        await service.open("short", init_data(OPERATOR))
    with pytest.raises(AccessDenied):
        await service.open("A" * 43, init_data(OPERATOR))
    assert store.jobs[job.id].state == "requested"


@pytest.mark.asyncio
async def test_a_token_is_bound_to_its_profile_and_to_a_current_operator() -> None:
    service, store, notifier, _ = flow()
    job = await announced(service, store)
    store.jobs[job.id] = replace(store.jobs[job.id], profile_id=str(uuid.uuid4()))
    with pytest.raises(AccessDenied, match="no longer open"):
        await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))
    assert events(store, job)[-1] == "access_denied"

    removed, store2, notifier2, _ = flow()
    job2 = await announced(removed, store2)
    demoted = VerificationService(store2, FakeLive(), FakeWatchdog(), notifier2, replace(removed.config, operator_ids=frozenset({OWNER})))
    with pytest.raises(AccessDenied):
        await demoted.open(token_of(notifier2.links(OPERATOR)[0]), init_data(OPERATOR))
    assert store2.jobs[job2.id].state == "requested"


@pytest.mark.asyncio
async def test_a_session_is_bound_to_its_job_and_to_a_current_operator() -> None:
    service, store, notifier, _ = flow()
    job = await announced(service, store)
    opened = await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))
    assert (await service.session(opened.cookie, job.id)).user_id == OPERATOR
    with pytest.raises(AccessDenied):
        await service.session(opened.cookie, str(uuid.uuid4()))
    with pytest.raises(AccessDenied):
        await service.session(None, job.id)
    demoted = VerificationService(store, FakeLive(), FakeWatchdog(), notifier, replace(service.config, operator_ids=frozenset({OWNER})))
    with pytest.raises(AccessDenied):
        await demoted.session(opened.cookie, job.id)


# --- claim, view, decisions ---------------------------------------------------------------


async def two_operators(service: VerificationService, notifier: FakeNotifier, job: Job):
    a = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    b = (await service.open(token_of(notifier.links(OTHER)[0]), init_data(OTHER))).session
    return a, b


@pytest.mark.asyncio
async def test_one_operator_claims_and_only_they_drive_the_job() -> None:
    live = FakeLive()
    service, store, notifier, _ = flow(live=live)
    job = await announced(service, store)
    a, b = await two_operators(service, notifier, job)

    with pytest.raises(ActionRefused, match="Claim"):
        await service.view(a)
    await service.claim(a)
    await service.claim(a)  # idempotent for the holder
    with pytest.raises(ActionRefused, match="already claimed"):
        await service.claim(b)
    for action in (service.view, service.solve, service.cancel, service.fail):
        with pytest.raises(ActionRefused, match="Another operator"):
            await action(b)

    assert await service.view(a) == "vncpass1"
    assert await service.view(a) == "vncpass1"
    assert live.started == [f"{job.profile_id}@{job.source_url}"]
    assert service.live_open(job.id)
    assert events(store, job)[-4:] == ["claim", "claim", "view", "view"]


@pytest.mark.asyncio
async def test_a_busy_browser_is_reported_not_crashed() -> None:
    service, store, notifier, _ = flow(live=FakeLive(fail=True))
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    with pytest.raises(ActionRefused, match="busy"):
        await service.view(session)
    assert store.jobs[job.id].state == "active"


@pytest.mark.asyncio
async def test_solved_is_confirmed_by_the_watchdog_then_the_run_resumes_once() -> None:
    live = FakeLive()
    service, store, notifier, watchdog = flow(Recovery(True), live=live)
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    await service.view(session)

    assert await service.solve(session) is True
    assert live.stopped == [job.profile_id]  # window closed before the watchdog takes the lease
    assert store.jobs[job.id].state == "verified"
    assert store.world[f"profile:{job.profile_id}"] == "ready"
    assert store.world[f"source:{job.source_id}"] == "active"
    assert store.world[f"batch:{job.batch_id}"] == "human_verification_required"  # not before Resume

    assert await service.resume(session) == job.batch_id
    assert watchdog.calls == 2  # checked again right before the run continues
    assert store.world[f"batch:{job.batch_id}"] == "queued" and store.world[f"item:{job.batch_id}"] == "queued"
    assert "starts automatically" in notifier.sent[-1][1]
    [launch] = store.launches.values()
    assert (launch.record.batch_id, launch.record.state, launch.record.notify_user_id) == (job.batch_id, "pending", OPERATOR)
    with pytest.raises(ActionRefused, match="once"):
        await service.resume(session)
    assert events(store, job)[-4:] == ["view", "solve", "recovery_confirmed", "resume"]


@pytest.mark.asyncio
async def test_a_challenge_still_showing_keeps_the_run_stopped() -> None:
    service, store, notifier, _ = flow(Recovery(False, kind="checkpoint", reason="facebook_url:/checkpoint"))
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    assert await service.solve(session) is False
    assert store.jobs[job.id].state == "active"
    with pytest.raises(ActionRefused, match="verified"):
        await service.resume(session)
    assert store.world[f"batch:{job.batch_id}"] == "human_verification_required"
    assert events(store, job)[-1] == "recovery_failed"


@pytest.mark.asyncio
async def test_a_challenge_that_returns_before_resume_blocks_it() -> None:
    service, store, notifier, _ = flow(Recovery(True), Recovery(False, kind="captcha", reason="facebook_page:captcha"))
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    assert await service.solve(session) is True
    with pytest.raises(ActionRefused, match="challenge again"):
        await service.resume(session)
    assert store.world[f"batch:{job.batch_id}"] == "human_verification_required"
    assert store.log[job.id][-1].detail["at"] == "resume"


@pytest.mark.asyncio
async def test_cancel_closes_the_job_and_its_stopped_batch() -> None:
    live = FakeLive()
    service, store, notifier, _ = flow(live=live)
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    await service.view(session)
    await service.cancel(session)
    assert store.jobs[job.id].state == "cancelled" and live.stopped
    assert store.world[f"batch:{job.batch_id}"] == "cancelled"
    assert all(t.revoked or t.used_at for t in store.tokens.values())
    with pytest.raises(AccessDenied):
        await service.session(None, job.id)
    assert events(store, job)[-1] == "cancel"


@pytest.mark.asyncio
async def test_failed_quarantines_the_profile_and_tells_the_owner() -> None:
    service, store, notifier, _ = flow()
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    await service.fail(session)
    assert store.jobs[job.id].state == "rejected"
    assert store.world[f"profile:{job.profile_id}"] == "quarantined"
    assert store.world[f"batch:{job.batch_id}"] == "failed"
    assert notifier.sent[-1][0] == OWNER and "marked failed" in notifier.sent[-1][1]
    with pytest.raises(ActionRefused, match="closed"):
        await service.cancel(session)


# --- sensitive cases ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("note", "kind"),
    [
        ("facebook_page:account restricted", "account_restricted"),
        ("facebook_page:account disabled", "account_restricted"),
        ("Please confirm your identity with a photo of your ID", "identity_verification"),
        ("Turn on two-factor authentication to continue", "two_factor_setup"),
    ],
)
async def test_sensitive_challenges_stop_and_go_to_the_owner_only(note: str, kind: str) -> None:
    live = FakeLive()
    service, store, notifier, _ = flow(live=live)
    job = await announced(service, store, new_job(note))
    assert store.jobs[job.id].sensitive and store.jobs[job.id].challenge_kind == kind
    assert store.world[f"profile:{job.profile_id}"] == "quarantined"
    assert not notifier.links(OPERATOR) and not notifier.links(OTHER)
    [(chat, text, button)] = notifier.sent
    assert chat == OWNER and "OWNER ACTION NEEDED" in text and button and button[0] == "Close verification job"
    assert "sensitive_stop" in events(store, job)

    owner = (await service.open(token_of(button[1]), init_data(OWNER))).session
    with pytest.raises(ActionRefused, match="closed"):
        await service.claim(owner)
    with pytest.raises(ActionRefused):
        await service.view(owner)
    await service.fail(owner)
    assert store.jobs[job.id].state == "rejected" and live.started == []


@pytest.mark.asyncio
async def test_a_sensitive_page_found_by_the_watchdog_stops_the_flow() -> None:
    service, store, notifier, _ = flow(Recovery(False, kind="identity_verification", sensitive=True, reason="upload id"))
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    assert await service.solve(session) is False
    assert store.jobs[job.id].sensitive
    assert store.world[f"profile:{job.profile_id}"] == "quarantined"
    assert notifier.sent[-1][0] == OWNER and "OWNER ACTION NEEDED" in notifier.sent[-1][1]
    with pytest.raises(AccessDenied):
        await service.session(None, job.id)
    with pytest.raises(ActionRefused, match="Only the owner"):
        await service.cancel(session)


# --- expiry and reminders --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unsolved_job_expires_and_its_window_closes() -> None:
    live = FakeLive()
    service, store, notifier, _ = flow(live=live)
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    await service.view(session)
    store.jobs[job.id] = replace(store.jobs[job.id], expires_at=datetime.now(UTC) - timedelta(seconds=1))
    await service.tick()
    assert store.jobs[job.id].state == "expired" and live.stopped == [job.profile_id]
    assert events(store, job)[-1] == "expire" and "expired" in notifier.sent[-1][1]


@pytest.mark.asyncio
async def test_unopened_links_are_renewed_after_the_reminder_interval() -> None:
    service, store, notifier, _ = flow()
    job = await announced(service, store)
    await service.tick()
    assert len(notifier.sent) == 3
    for record in store.tokens.values():
        record.created_at = datetime.now(UTC) - timedelta(hours=1)
    await service.tick()
    assert len(notifier.sent) == 6 and notifier.sent[-1][1].startswith("Reminder:")
    # Someone with an open page session gets no reminders.
    await service.open(token_of(notifier.links(OPERATOR)[-1]), init_data(OPERATOR))
    for record in store.tokens.values():
        record.created_at = datetime.now(UTC) - timedelta(hours=1)
    await service.tick()
    assert len(notifier.sent) == 6
    assert events(store, job).count("notified") == 6


# --- classification and watchdog judgement ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("facebook_url:/checkpoint", ("checkpoint", False)),
        ("facebook_page:captcha", ("captcha", False)),
        ("facebook_url:/login", ("login", False)),
        ("facebook_page:we detected automated behavior", ("account_warning", False)),
        ("facebook_url:/two_factor", ("checkpoint", False)),  # entering an existing code is not enrolment
        ("Your account has been locked", ("account_restricted", True)),
        ("Подтвердите свою личность", ("identity_verification", True)),
        ("Security check. Confirm your identity", ("identity_verification", True)),  # sensitive wins
        ("", ("unknown", False)),
        (None, ("unknown", False)),
    ],
)
def test_classification(text: str | None, expected: tuple[str, bool]) -> None:
    assert classify(text) == expected


def test_the_watchdog_needs_a_clean_page() -> None:
    assert judge({"url": "https://www.facebook.com/groups/plots", "title": "Plots", "text": "Nice plot for sale"}).clear
    blocked = judge({"url": "https://www.facebook.com/checkpoint/123", "title": "", "text": ""})
    assert not blocked.clear and blocked.kind == "checkpoint"
    restricted = judge({"url": "https://www.facebook.com/", "title": "", "text": "Your account has been disabled"})
    assert not restricted.clear and restricted.sensitive


# --- settings ------------------------------------------------------------------------------------


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    base = {
        "DATABASE_URL": "postgresql://x", "TELEGRAM_TOKEN": "1:x", "TELEGRAM_OPERATOR_IDS": "11,12",
        "VERIFICATION_PUBLIC_URL": PUBLIC, "BROWSER_SESSION_API_TOKEN": "t" * 32,
    }
    for key in (*base, "VERIFICATION_OWNER_TELEGRAM_ID", "LIVE_VIEW_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**base, **values}.items():
        monkeypatch.setenv(key, value)


def test_settings_default_the_owner_and_keep_secrets_out_of_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    settings = VerificationSettings.from_env()
    assert settings.owner_id == 11 and settings.public_url == PUBLIC
    assert "1:x" not in repr(settings)


def test_the_public_url_falls_back_to_the_live_view_one_and_may_be_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, VERIFICATION_PUBLIC_URL="", LIVE_VIEW_PUBLIC_URL="https://5-6-7-8.sslip.io/")
    assert VerificationSettings.from_env().public_url == "https://5-6-7-8.sslip.io"
    _env(monkeypatch, VERIFICATION_PUBLIC_URL="")
    assert VerificationSettings.from_env().public_url == ""  # idle, not a crash loop


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"VERIFICATION_PUBLIC_URL": "http://1-2-3-4.sslip.io"}, "https://host"),
        ({"VERIFICATION_PUBLIC_URL": "https://1-2-3-4.sslip.io/verify"}, "https://host"),
        ({"TELEGRAM_OPERATOR_IDS": ""}, "at least one"),
        ({"TELEGRAM_OPERATOR_IDS": "@owner"}, "numeric"),
        ({"VERIFICATION_OWNER_TELEGRAM_ID": "99"}, "must also be"),
        ({"VERIFICATION_TOKEN_MINUTES": "0"}, "between"),
    ],
)
def test_unsafe_settings_stop_startup(monkeypatch: pytest.MonkeyPatch, values: dict[str, str], match: str) -> None:
    _env(monkeypatch, **values)
    with pytest.raises(ValueError, match=match):
        VerificationSettings.from_env()


def test_https_origin_strips_the_slash() -> None:
    assert https_origin(PUBLIC + "/") == PUBLIC


async def resumed(service, store, notifier):
    job = await announced(service, store)
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), init_data(OPERATOR))).session
    await service.claim(session)
    await service.solve(session)
    await service.resume(session)
    [launch_id] = store.launches
    return job, launch_id


@pytest.mark.asyncio
async def test_the_restarted_batch_is_reported_once_per_state() -> None:
    service, store, notifier, _ = flow(Recovery(True))
    job, launch_id = await resumed(service, store, notifier)
    before = len(notifier.sent)
    await service.tick()
    assert len(notifier.sent) == before  # pending and not yet stale: quiet

    store.set_launch(launch_id, "running")
    await service.tick()
    await service.tick()
    assert [(c, t) for c, t, _ in notifier.sent[before:]] == [(OPERATOR, f"Batch {job.batch_id} started automatically.")]

    store.set_launch(launch_id, "finished", result="succeeded")
    await service.tick()
    assert notifier.sent[-1][:2] == (OPERATOR, f"Batch {job.batch_id} finished: all its groups were read.")
    assert len(notifier.sent) == before + 2
    assert [e.detail.get("launch") for e in store.log[job.id] if e.actor == "verification:runner"] == ["running", "finished"]


@pytest.mark.asyncio
async def test_a_failed_restart_also_tells_the_owner_with_the_manual_command() -> None:
    service, store, notifier, _ = flow(Recovery(True))
    job, launch_id = await resumed(service, store, notifier)
    store.set_launch(launch_id, "failed", error="ValueError: no ready profile")
    await service.tick()
    sent = {c: t for c, t, _ in notifier.sent[-2:]}
    assert set(sent) == {OWNER, OPERATOR}
    assert f"FACEBOOK_BATCH_ID={job.batch_id}" in sent[OWNER] and "no ready profile" in sent[OWNER]


@pytest.mark.asyncio
async def test_a_launch_nobody_picks_up_is_reported_as_stale_once() -> None:
    service, store, notifier, _ = flow(Recovery(True))
    job, launch_id = await resumed(service, store, notifier)
    store.launches[launch_id].requested_at -= timedelta(minutes=6)
    before = len(notifier.sent)
    await service.tick()
    await service.tick()
    stale = notifier.sent[before:]
    assert {c for c, _, _ in stale} == {OWNER, OPERATOR} and len(stale) == 2
    assert all("facebook-runner" in t and f"FACEBOOK_BATCH_ID={job.batch_id}" in t for _, t, _ in stale)
