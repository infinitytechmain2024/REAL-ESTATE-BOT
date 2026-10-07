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
import hmac
import logging
import secrets
import time
from collections.abc import Collection
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from bot.operators import OperatorSet
from bot.telegram_webapp import verify_init_data

from .browser import BrowserUnavailable, LiveBrowser, RecoveryChecker
from .classify import SENSITIVE_KINDS, classify
from .models import Job, Launch, PageSession
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
    # Everyone who handles verification: .env owners plus approved helpers and
    # operators (an OperatorSet, refreshed from the database on every tick).
    operator_ids: Collection[int]
    owner_id: int
    # Verifies the Mini App signature; never shown or logged.
    bot_token: str = field(repr=False)
    token_minutes: int = 15
    session_minutes: int = 30
    job_hours: int = 24
    renotify_minutes: int = 30
    live_minutes: int = 20
    # A resumed batch not started by facebook-runner within this time is
    # reported with the manual command.
    launch_stale_minutes: int = 5


@dataclass(frozen=True)
class Opened:
    session: PageSession
    cookie: str


@dataclass
class _BrowserRequest:
    """A link opened outside Telegram, waiting for its recipient to approve it in Telegram."""

    token_sha: str
    user_id: int
    job_id: str
    approval_sha: str
    expires_at: float
    approved: bool = False


BROWSER_REQUEST_SECONDS = 600
MAX_BROWSER_REQUESTS_PER_LINK = 3


class VerificationService:
    def __init__(self, store: VerificationStore, live: LiveBrowser, watchdog: RecoveryChecker, notifier: Notifier, config: FlowConfig) -> None:
        self.store, self.live, self.watchdog, self.notifier, self.config = store, live, watchdog, notifier, config
        # One-time VNC passwords of open windows, by job; never persisted.
        self._live_passwords: dict[str, str] = {}
        # Browser logins waiting for approval, by the hash of the waiting browser's cookie.
        # Lost on restart, which only means opening the link again.
        self._browser: dict[str, _BrowserRequest] = {}
        self._lock = asyncio.Lock()

    # --- detection and notification -------------------------------------------------

    async def tick(self) -> None:
        # Approvals and revocations made in Telegram take effect here within one tick.
        if isinstance(self.config.operator_ids, OperatorSet):
            self.config.operator_ids.replace_approved(await self.store.approved_roles())
        for job in await self.store.expire_due("verification:expiry"):
            await self._expired(job)
        for job in await self.store.unannounced_jobs():
            await self._announce(job)
        for job in await self.store.jobs_needing_reminder(self.config.renotify_minutes * 60):
            await self._send_links(job, reminder=True)
        for launch in await self.store.launch_updates(self.config.launch_stale_minutes * 60):
            await self._report_launch(launch)

    async def _report_launch(self, launch: Launch) -> None:
        manual = f"FACEBOOK_BATCH_ID={launch.batch_id} docker compose --profile collector up facebook-collector"
        if launch.stale:
            text = (f"Batch {launch.batch_id} has not started automatically after {self.config.launch_stale_minutes} minutes; "
                    f"is facebook-runner running? Start it by hand on the VPS:\n{manual}")
        elif launch.state == "running":
            text = f"Batch {launch.batch_id} started automatically."
        elif launch.state == "finished":
            text = {
                "succeeded": f"Batch {launch.batch_id} finished: all its groups were read.",
                "cancelled": f"Batch {launch.batch_id} was cancelled while it ran.",
                "human_verification_required": f"Batch {launch.batch_id} stopped at a new challenge; a verification notice follows.",
            }.get(launch.result or "", f"Batch {launch.batch_id} finished: {launch.result}.")
        elif launch.state == "skipped":
            text = f"Batch {launch.batch_id} was not restarted: {launch.error or 'it is no longer queued'}."
        else:
            text = f"Batch {launch.batch_id} failed after the restart ({launch.error or 'unknown error'}). Run it by hand after checking:\n{manual}"
        recipients = {launch.notify_user_id} if launch.notify_user_id else set()
        if launch.stale or launch.state in {"failed", "skipped"}:
            recipients.add(self.config.owner_id)
        # Mark first: a Telegram outage must not turn into a message on every tick.
        await self.store.mark_launch_notified(launch.id, "stale" if launch.stale else launch.state)
        if launch.job_id:
            await self.store.add_event(launch.job_id, "resume", "verification:runner",
                                       {"batch_id": launch.batch_id, "launch": "stale" if launch.stale else launch.state,
                                        "result": launch.result, "error": launch.error})
        for user_id in sorted(recipients):
            with suppress(Exception):
                await self.notifier.send(user_id, text)

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
        website = job.platform == "website"
        text = (self._website_text(job, reminder) if website else
                self._facebook_text(job, what, reminder))
        for user_id in recipients:
            secret, sha = new_secret()
            await self.store.issue_token(job.id, user_id, job.profile_id, sha, self.config.token_minutes * 60)
            await self.store.add_event(job.id, "token_issued", "verification", {"user_id": user_id, "reminder": reminder})
            link = self._link(secret)
            browser = (self._website_browser_text(link) if website else
                       f"\n\nOr copy this link into Safari or another browser; you will confirm it here in Telegram "
                       f"before it opens. Do not forward it.\n{link}")
            try:
                await self.notifier.send(user_id, text + browser,
                                         ("Открыть проверку" if website else "Open verification page", link))
                await self.store.add_event(job.id, "notified", "verification", {"user_id": user_id, "reminder": reminder})
            except Exception as exc:  # noqa: BLE001 - one unreachable operator must not stop the others
                log.warning("verification.notify_failed", extra={"user_id": user_id, "error": type(exc).__name__})

    def _facebook_text(self, job: Job, what: str, reminder: bool) -> str:
        return (
            f"{'Reminder: ' if reminder else ''}{job.platform.capitalize()} showed {what} on profile {job.profile_name} "
            f"while reading {job.source_url}. Collection on that profile is stopped.\n\n"
            f"Open the verification page from the button below. It works for you only, once, and expires in "
            f"{self.config.token_minutes} minutes; the job expires {self._expiry_text(job)}."
        )

    def _website_text(self, job: Job, reminder: bool) -> str:
        return (
            f"{'Напоминание. ' if reminder else ''}Сайт {job.host} просит пройти проверку. Откройте браузер, "
            f"пройдите её и нажмите «Готово».\n\n"
            f"Кнопка работает только для вас, один раз и {self.config.token_minutes} мин.; "
            f"задача истекает {self._expiry_text(job)}. Пока её не пройдут, сайт в поиске пропускается."
        )

    @staticmethod
    def _website_browser_text(link: str) -> str:
        return ("\n\nИли скопируйте ссылку в Safari или другой браузер: сначала вы подтвердите её здесь, в Telegram, "
                f"и только потом она откроется. Не пересылайте её.\n{link}")

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
            button = ("Close verification job", self._link(secret))
        with suppress(Exception):
            await self.notifier.send(self.config.owner_id, text, button)
            await self.store.add_event(job.id, "notified", "verification", {"user_id": self.config.owner_id, "owner": True})

    async def _expired(self, job: Job) -> None:
        await self._close_window(job)
        await self.store.add_event(job.id, "expire", "verification:expiry", {})
        text = (f"Проверку сайта {job.host} никто не прошёл: сайт не читается в этом поиске."
                if job.platform == "website" else
                f"Verification job {job.id} for profile {job.profile_name} expired unsolved; its run stays stopped.")
        with suppress(Exception):
            await self.notifier.send(self.config.owner_id, text)

    def _link(self, secret: str) -> str:
        return f"{self.config.public_url}/verify/v/{secret}"

    @staticmethod
    def _expiry_text(job: Job) -> str:
        return f"at {job.expires_at:%Y-%m-%d %H:%M} UTC" if job.expires_at else "later"

    # --- access -----------------------------------------------------------------------

    def telegram_user(self, init_data: str | None) -> int:
        """The operator Telegram signed into the Mini App; checked before anything else."""
        user_id = verify_init_data(init_data or "", self.config.bot_token)
        if user_id is None:
            raise AccessDenied("Open this page from the button in the Telegram bot.")
        if user_id not in self.config.operator_ids:
            raise AccessDenied("Only operators can open verification jobs.")
        return user_id

    async def open(self, token: str, init_data: str | None) -> Opened:
        user_id = self.telegram_user(init_data)
        if not looks_like_secret(token):
            raise AccessDenied("This link is invalid.")
        return await self._consume(digest(token), user_id, "telegram")

    # --- the same link in an ordinary browser, approved from Telegram ---------------

    def _browser_prune(self) -> None:
        now = time.time()
        for key in [k for k, r in self._browser.items() if r.expires_at < now]:
            del self._browser[key]

    async def request_browser(self, token: str, client: str) -> str:
        """Someone opened the link outside Telegram: ask its recipient; returns the waiting browser's cookie."""
        if not looks_like_secret(token):
            raise AccessDenied("This link is invalid.")
        token_sha = digest(token)
        record = await self.store.peek_token(token_sha)
        if record is None:
            raise AccessDenied("This link was already used or has expired. Wait for a new message.")
        if record.user_id not in self.config.operator_ids:
            raise AccessDenied("This link is no longer valid.")
        self._browser_prune()
        if sum(1 for r in self._browser.values() if r.token_sha == token_sha) >= MAX_BROWSER_REQUESTS_PER_LINK:
            raise AccessDenied("Too many attempts with this link. Wait for a new message.")
        cookie, cookie_sha = new_secret()
        approval, approval_sha = new_secret()
        self._browser[cookie_sha] = _BrowserRequest(token_sha, record.user_id, record.job_id, approval_sha,
                                                   time.time() + BROWSER_REQUEST_SECONDS)
        await self.notifier.send(record.user_id, (
            f"Your verification link was opened in a browser ({client[:120]}).\n\n"
            "If that was you, press Approve and go back to that browser. If not, ignore this message: "
            "nothing opens without your approval."
        ), ("Approve browser login", f"{self.config.public_url}/verify/a/{approval}"))
        return cookie

    async def approve_browser(self, approval: str, init_data: str | None) -> None:
        """The recipient pressed Approve in Telegram (signed Mini App)."""
        user_id = self.telegram_user(init_data)
        if not looks_like_secret(approval):
            raise AccessDenied("This approval is invalid.")
        self._browser_prune()
        approval_sha = digest(approval)
        for request in self._browser.values():
            if hmac.compare_digest(request.approval_sha, approval_sha):
                if request.user_id != user_id:
                    await self.store.add_event(request.job_id, "access_denied", f"telegram:{user_id}", {"reason": "browser_approval_by_other"})
                    raise AccessDenied("Only the person the link was sent to can approve it.")
                request.approved = True
                return
        raise AccessDenied("This approval has expired. Open the link in the browser again.")

    async def browser_status(self, token: str, cookie: str | None) -> Opened | None:
        """For the waiting browser: None while not approved, then its page session (once)."""
        if not looks_like_secret(token) or not cookie or not looks_like_secret(cookie):
            raise AccessDenied("Open the link again.")
        self._browser_prune()
        request = self._browser.get(digest(cookie))
        if request is None or not hmac.compare_digest(request.token_sha, digest(token)):
            raise AccessDenied("This browser request has expired. Open the link again.")
        if not request.approved:
            return None
        del self._browser[digest(cookie)]
        return await self._consume(request.token_sha, request.user_id, "browser")

    async def _consume(self, token_sha: str, user_id: int, via: str) -> Opened:
        identity = f"telegram:{user_id}"
        record = await self.store.consume_token(token_sha, user_id, identity)
        if record is None:
            raise AccessDenied("This link was already used, has expired, or was sent to someone else. Wait for a new message.")
        job = await self.store.get_job(record.job_id)
        # Bound to job, user and profile: all three must still match.
        # Sensitive jobs open for the owner alone (to close them), never for anyone else.
        owner_only = job is not None and job.sensitive and record.user_id != self.config.owner_id
        if job is None or not job.is_open or owner_only or record.user_id not in self.config.operator_ids or job.profile_id != record.profile_id:
            await self.store.add_event(record.job_id, "access_denied", identity, {"user_id": record.user_id, "reason": "binding"})
            raise AccessDenied("This verification is no longer open.")
        cookie, cookie_sha = new_secret()
        session = await self.store.create_session(record, identity, cookie_sha, secrets.token_urlsafe(24), self.config.session_minutes * 60)
        await self.store.add_event(job.id, "opened", self._actor(session), {"via": via})
        return Opened(session, cookie)

    async def session(self, cookie: str | None, job_id: str) -> PageSession:
        if not cookie or not looks_like_secret(cookie):
            raise AccessDenied("Open this page from the button in the Telegram bot.")
        session = await self.store.get_session(digest(cookie))
        # Re-checked on every request: removing an operator ends their session.
        if session is None or session.job_id != job_id or session.user_id not in self.config.operator_ids:
            raise AccessDenied("This page session is not valid. Use a new Telegram link.")
        return session

    @staticmethod
    def _actor(session: PageSession) -> str:
        return f"telegram:{session.user_id}"

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
            if job.id in self._live_passwords and not await self.live.is_open(job.profile_id or ""):
                # The browser service restarted and closed the window: open it again.
                self._live_passwords.pop(job.id, None)
            if job.id not in self._live_passwords:
                try:
                    self._live_passwords[job.id] = await self.live.start(job.profile_id or "", job.profile_name or "", job.platform, job.page_url, self.config.live_minutes)
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
            cleared = await self._watch(job, actor)
            if cleared and job.platform == "website":
                await self._continue_website(job, actor, session.user_id)
            return cleared

    async def _continue_website(self, job: Job, actor: str, user_id: int) -> None:
        """A website job has no run to requeue: the web stage sees the verified job and reads the site again."""
        await self.store.resume(job.id, actor, user_id)
        await self.store.add_event(job.id, "resume", actor, {"website": job.host})
        with suppress(Exception):
            await self.notifier.send(user_id, f"Проверка сайта {job.host} пройдена. Продолжаю читать его, не спеша.")

    async def _watch(self, job: Job, actor: str) -> bool:
        recovery = await self.watchdog.check(job.profile_id or "", job.profile_name or "", job.platform, job.page_url)
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
            recovery = await self.watchdog.check(job.profile_id or "", job.profile_name or "", job.platform, job.page_url)
            if not recovery.clear:
                await self.store.add_event(job.id, "recovery_failed", "verification:watchdog", {"kind": recovery.kind, "reason": recovery.reason, "at": "resume"})
                raise ActionRefused("The browser shows a challenge again; the run was not resumed.")
            batch_id = await self.store.resume(job.id, actor, session.user_id)
            await self.store.add_event(job.id, "resume", actor, {"batch_id": batch_id})
        with suppress(Exception):
            await self.notifier.send(session.user_id, (
                f"Verification {job.id} solved and confirmed. Batch {batch_id} is queued again from the group that was "
                f"challenged and starts automatically; you will get a message when it finishes."
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
