"""Rules of the human verification flow; transport-free and fully audited.

Lifecycle of a job (states from migration 003):

    requested --claim--> active --solve + watchdog clear--> verified --resume--> (run requeued)
         \\                   \\--fail--> rejected (batch failed, profile quarantined)
          \\--cancel / expire--> cancelled / expired (batch cancelled / left for later)

Sensitive challenges (identity, new 2FA, restriction) never reach "active":
the profile is quarantined, every link is revoked and only the owner is told.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .browser import BrowserUnavailable, LiveBrowser, RecoveryChecker
from .classify import SENSITIVE_KINDS, classify
from .models import Job, PageSession
from .store import VerificationStore
from .telegram import Notifier
from .tokens import digest, looks_like_secret, new_secret

log = logging.getLogger(__name__)

KIND_TEXT = {
    "checkpoint": "a security checkpoint",
    "captcha": "a CAPTCHA",
    "login": "a login page",
    "account_warning": "an account warning",
    "identity_verification": "an identity verification (ID or selfie)",
    "two_factor_setup": "a request to set up two-factor authentication",
    "account_restricted": "an account restriction",
    "unknown": "a challenge",
}


class AccessDenied(PermissionError):
    """The message is safe to show on the page."""


class ActionRefused(RuntimeError):
    """The message is safe to show on the page."""


@dataclass(frozen=True)
class FlowConfig:
    public_url: str
    operator_ids: frozenset[int]
    owner_id: int
    tailscale_logins: frozenset[str]
    token_minutes: int = 15
    session_minutes: int = 30
    job_hours: int = 24
    renotify_minutes: int = 30
    live_minutes: int = 20


@dataclass(frozen=True)
class Opened:
    session: PageSession
    cookie: str


class VerificationService:
    def __init__(self, store: VerificationStore, live: LiveBrowser, watchdog: RecoveryChecker, notifier: Notifier, config: FlowConfig) -> None:
        self.store, self.live, self.watchdog, self.notifier, self.config = store, live, watchdog, notifier, config
        # One-time VNC passwords of open windows, by job; never persisted.
        self._live_passwords: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # --- detection and notification -------------------------------------------------

    async def tick(self) -> None:
        for job in await self.store.expire_due("verification:expiry"):
            await self._expired(job)
        for job in await self.store.unannounced_jobs():
            await self._announce(job)
        for job in await self.store.jobs_needing_reminder(self.config.renotify_minutes * 60):
            await self._send_links(job, reminder=True)

    async def _announce(self, job: Job) -> None:
        kind, sensitive = classify(job.resolution_note)
        job = await self.store.mark_announced(job.id, job.profile_id, kind, sensitive, self.config.job_hours * 3600)
        await self.store.add_event(job.id, "detected", "verification", {"kind": kind, "sensitive": sensitive, "reason": job.resolution_note})
        if sensitive or job.profile_id is None:
            await self._stop_sensitive(job, kind if sensitive else "unknown", "verification")
            return
        await self._send_links(job, reminder=False)

    async def _send_links(self, job: Job, *, reminder: bool) -> None:
        assert job.profile_id is not None
        recipients = [job.claimed_by] if job.claimed_by else sorted(self.config.operator_ids)
        what = KIND_TEXT.get(job.challenge_kind or "unknown", "a challenge")
        text = (
            f"{'Reminder: ' if reminder else ''}{job.platform.capitalize()} showed {what} on profile {job.profile_name} "
            f"while reading {job.source_url}. Collection on that profile is stopped.\n\n"
            f"Open the private verification page (Tailscale only). The link works once and expires in "
            f"{self.config.token_minutes} minutes; the job expires {self._expiry_text(job)}."
        )
        for user_id in recipients:
            secret, sha = new_secret()
            await self.store.issue_token(job.id, user_id, job.profile_id, sha, self.config.token_minutes * 60)
            await self.store.add_event(job.id, "token_issued", "verification", {"user_id": user_id, "reminder": reminder})
            try:
                await self.notifier.send(user_id, text, ("Open verification page", f"{self.config.public_url}/v/{secret}"))
                await self.store.add_event(job.id, "notified", "verification", {"user_id": user_id, "reminder": reminder})
            except Exception as exc:  # noqa: BLE001 - one unreachable operator must not stop the others
                log.warning("verification.notify_failed", extra={"user_id": user_id, "error": type(exc).__name__})

    async def _stop_sensitive(self, job: Job, kind: str, actor: str) -> None:
        await self._close_window(job)
        await self.store.sensitive_stop(job.id, kind, actor)
        await self.store.add_event(job.id, "sensitive_stop", actor, {"kind": kind})
        what = KIND_TEXT.get(kind, "a challenge")
        text = (
            f"OWNER ACTION NEEDED. {job.platform.capitalize()} showed {what} on profile {job.profile_name or '(none linked)'} "
            f"({job.source_url}). This is about the account itself, so nothing is automated and no live browser is "
            f"offered: the profile is quarantined and its collection stopped.\n\n"
            f"Handle it on the account directly, then close the job as Failed or Cancelled on the page below "
            f"(owner only, one-time link, {self.config.token_minutes} minutes) and log the profile in again with /login."
        )
        button = None
        if job.profile_id:
            secret, sha = new_secret()
            await self.store.issue_token(job.id, self.config.owner_id, job.profile_id, sha, self.config.token_minutes * 60)
            await self.store.add_event(job.id, "token_issued", "verification", {"user_id": self.config.owner_id, "owner": True})
            button = ("Close verification job", f"{self.config.public_url}/v/{secret}")
        with suppress(Exception):
            await self.notifier.send(self.config.owner_id, text, button)
            await self.store.add_event(job.id, "notified", "verification", {"user_id": self.config.owner_id, "owner": True})

    async def _expired(self, job: Job) -> None:
        await self._close_window(job)
        await self.store.add_event(job.id, "expire", "verification:expiry", {})
        with suppress(Exception):
            await self.notifier.send(self.config.owner_id, f"Verification job {job.id} for profile {job.profile_name} expired unsolved; its run stays stopped.")

    @staticmethod
    def _expiry_text(job: Job) -> str:
        return f"at {job.expires_at:%Y-%m-%d %H:%M} UTC" if job.expires_at else "later"

    # --- access -----------------------------------------------------------------------

    def check_login(self, login: str | None) -> str:
        """The Tailscale identity that ``tailscale serve`` put on the request."""
        normalized = (login or "").strip().lower()
        if not normalized or normalized not in self.config.tailscale_logins:
            raise AccessDenied("This page is only for approved Tailscale users.")
        return normalized

    async def open(self, token: str, login: str | None) -> Opened:
        tailscale_login = self.check_login(login)
        if not looks_like_secret(token):
            raise AccessDenied("This link is invalid.")
        record = await self.store.consume_token(digest(token), tailscale_login)
        if record is None:
            raise AccessDenied("This link was already used or has expired. Wait for a new message.")
        job = await self.store.get_job(record.job_id)
        # Bound to job, user and profile: all three must still match.
        # Sensitive jobs open for the owner alone (to close them), never for anyone else.
        owner_only = job is not None and job.sensitive and record.user_id != self.config.owner_id
        if job is None or not job.is_open or owner_only or record.user_id not in self.config.operator_ids or job.profile_id != record.profile_id:
            await self.store.add_event(record.job_id, "access_denied", tailscale_login, {"user_id": record.user_id, "reason": "binding"})
            raise AccessDenied("This verification is no longer open.")
        cookie, cookie_sha = new_secret()
        session = await self.store.create_session(record, tailscale_login, cookie_sha, secrets.token_urlsafe(24), self.config.session_minutes * 60)
        await self.store.add_event(job.id, "opened", self._actor(session), {})
        return Opened(session, cookie)

    async def session(self, cookie: str | None, job_id: str, login: str | None) -> PageSession:
        tailscale_login = self.check_login(login)
        if not cookie or not looks_like_secret(cookie):
            raise AccessDenied("Open this page from the Telegram link.")
        session = await self.store.get_session(digest(cookie))
        if session is None or session.job_id != job_id or session.tailscale_login != tailscale_login or session.user_id not in self.config.operator_ids:
            raise AccessDenied("This page session is not valid. Use a new Telegram link.")
        return session

    @staticmethod
    def _actor(session: PageSession) -> str:
        return f"telegram:{session.user_id}/tailscale:{session.tailscale_login}"

    async def page(self, session: PageSession) -> tuple[Job, list[Any]]:
        job = await self.store.get_job(session.job_id)
        if job is None:
            raise AccessDenied("Unknown job.")
        return job, await self.store.events(job.id)

    # --- actions ------------------------------------------------------------------------

    async def _job(self, session: PageSession) -> Job:
        job = await self.store.get_job(session.job_id)
        if job is None or job.profile_id != session.profile_id:
            raise AccessDenied("Unknown job.")
        return job

    def _require_claimant(self, job: Job, session: PageSession) -> None:
        if job.state != "active" or job.claimed_by != session.user_id:
            raise ActionRefused("Claim the job first." if job.state == "requested" else "Another operator holds this job.")

    async def claim(self, session: PageSession) -> None:
        async with self._lock:
            job = await self._job(session)
            if not job.is_open or job.sensitive:
                raise ActionRefused("This job is closed.")
            if not await self.store.claim(job.id, session.user_id, self._actor(session)):
                raise ActionRefused("Another operator already claimed this job.")
            await self.store.add_event(job.id, "claim", self._actor(session), {})

    async def view(self, session: PageSession) -> str:
        """Open (or reuse) the profile's live browser; returns the VNC password."""
        async with self._lock:
            job = await self._job(session)
            self._require_claimant(job, session)
            if job.id not in self._live_passwords:
                try:
                    self._live_passwords[job.id] = await self.live.start(job.profile_id or "", job.profile_name or "", job.platform, job.source_url, self.config.live_minutes)
                except BrowserUnavailable as exc:
                    raise ActionRefused(f"Cannot open the browser: {exc}.") from exc
            await self.store.add_event(job.id, "view", self._actor(session), {})
            return self._live_passwords[job.id]

    def live_open(self, job_id: str) -> bool:
        return job_id in self._live_passwords

    async def _close_window(self, job: Job) -> None:
        if self._live_passwords.pop(job.id, None) is not None and job.profile_id:
            await self.live.stop(job.profile_id)

    async def solve(self, session: PageSession) -> bool:
        """The human says done; only the watchdog decides whether it is."""
        async with self._lock:
            job = await self._job(session)
            self._require_claimant(job, session)
            actor = self._actor(session)
            # Closing the window writes the new login state into the profile and
            # frees its lease for the watchdog.
            await self._close_window(job)
            await self.store.mark_solved(job.id, actor)
            await self.store.add_event(job.id, "solve", actor, {})
            return await self._watch(job, actor)

    async def _watch(self, job: Job, actor: str) -> bool:
        recovery = await self.watchdog.check(job.profile_id or "", job.profile_name or "", job.platform, job.source_url)
        if recovery.clear:
            await self.store.confirm_recovery(job.id, actor)
            await self.store.add_event(job.id, "recovery_confirmed", "verification:watchdog", {})
            return True
        await self.store.add_event(job.id, "recovery_failed", "verification:watchdog", {"kind": recovery.kind, "reason": recovery.reason})
        if recovery.sensitive and (recovery.kind or "") in SENSITIVE_KINDS:
            await self._stop_sensitive(job, recovery.kind or "unknown", "verification:watchdog")
        return False

    async def resume(self, session: PageSession) -> str | None:
        async with self._lock:
            job = await self._job(session)
            if job.state != "verified" or job.resumed_at is not None:
                raise ActionRefused("Only a verified job can resume its run, once.")
            if job.claimed_by != session.user_id:
                raise ActionRefused("Only the operator who solved this job can resume it.")
            actor = self._actor(session)
            # The watchdog looks again right before the run continues.
            recovery = await self.watchdog.check(job.profile_id or "", job.profile_name or "", job.platform, job.source_url)
            if not recovery.clear:
                await self.store.add_event(job.id, "recovery_failed", "verification:watchdog", {"kind": recovery.kind, "reason": recovery.reason, "at": "resume"})
                raise ActionRefused("The browser shows a challenge again; the run was not resumed.")
            batch_id = await self.store.resume(job.id, actor)
            await self.store.add_event(job.id, "resume", actor, {"batch_id": batch_id})
        with suppress(Exception):
            await self.notifier.send(session.user_id, (
                f"Verification {job.id} solved and confirmed. Batch {batch_id} is queued again from the group that was "
                f"challenged. Start it on the VPS:\nFACEBOOK_BATCH_ID={batch_id} docker compose --profile collector up facebook-collector"
            ) if batch_id else f"Verification {job.id} solved and confirmed; the profile is ready.")
        return batch_id

    async def cancel(self, session: PageSession) -> None:
        await self._close(session, "cancel")

    async def fail(self, session: PageSession) -> None:
        await self._close(session, "fail")

    async def _close(self, session: PageSession, action: str) -> None:
        async with self._lock:
            job = await self._job(session)
            if not job.is_open:
                raise ActionRefused("This job is already closed.")
            if job.sensitive and session.user_id != self.config.owner_id:
                raise ActionRefused("Only the owner can close this job.")
            if job.state == "active" and job.claimed_by != session.user_id and session.user_id != self.config.owner_id:
                raise ActionRefused("Another operator holds this job.")
            actor = self._actor(session)
            await self._close_window(job)
            done = await (self.store.cancel(job.id, actor) if action == "cancel" else self.store.fail(job.id, actor))
            if not done:
                raise ActionRefused("This job is already closed.")
            await self.store.add_event(job.id, action, actor, {})
        if action == "fail":
            with suppress(Exception):
                await self.notifier.send(self.config.owner_id, f"Verification {job.id} on profile {job.profile_name} was marked failed by operator {session.user_id}. The profile is quarantined and its batch failed.")
