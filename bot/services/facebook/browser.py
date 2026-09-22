"""The shared Facebook browser session.

Exactly one browser drives every Facebook operation -- group reading,
comment reading, eventually commenting. It is never headless: the whole
point of a visible window is that a human can take it over the moment
automatic login fails, without a second profile or a second login. See the
implementation plan's login-recovery state machine; this module implements
the browser half of it.

Two ways to get that browser, chosen by whether ``FacebookSettings.cdp_url``
is set:

- **Local dev (no cdp_url):** Playwright launches and owns a persistent
  Chrome profile itself (``launch_persistent_context``). Simple, but the
  browser dies with this process.
- **VM/production (cdp_url set):** a real system Chrome is started and
  supervised separately (systemd/compose), with remote debugging enabled on
  that URL, and this process only *attaches* to it over CDP
  (``connect_over_cdp``) -- it never launches a second browser. This is what
  lets a noVNC viewer and this bot look at the exact same window: there is
  only ever one Chrome process, one profile, one login state. Stopping this
  session in that mode disconnects Playwright's client; it does not close
  the remote Chrome, which is not this process's to close.

This module owns the browser lifecycle and the coarse login state. It does
not know about groups or posts -- see ``bot.services.facebook.groups`` for
that.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from enum import StrEnum
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from bot.config import FacebookSettings
from bot.logging_conf import get_logger

log = get_logger(__name__)

FACEBOOK_HOME = "https://www.facebook.com/"

# Selectors and text signals below are a starting point, not a verified
# contract: Facebook changes its markup often, by market and by rollout.
# Confirm every one of these against a real logged-in session before relying
# on it (see scripts/facebook_probe.py), and prefer role/text/placeholder
# locators over class names, which churn faster.
LOGIN_FORM_SELECTOR = "form[data-testid='royal_login_form'], #login_form"
LOGGED_IN_MARKER_SELECTOR = "[aria-label='Facebook'], div[role='navigation']"

# URL path fragments that mean "not a normal logged-in page", checked
# case-insensitively against the whole URL.
CHECKPOINT_PATH_FRAGMENTS = ("/checkpoint", "/login", "recover")

# Visible-text signals for a challenge screen that doesn't necessarily show
# up as a distinct URL (e.g. an inline "confirm it's you" panel on the home
# page). English only for now -- the browser context is forced to en-US, so
# this is what Facebook should show; it will need updating if that changes.
CHALLENGE_TEXT_SIGNALS = (
    "confirm it's you",
    "enter the characters you see",
    "we suspect automated behavior",
    "upload id",
    "try another way",
    "suspicious activity",
)

# Facebook's own logged-in session cookies. Their presence is a necessary
# but not sufficient signal on its own -- paired with the logged-in marker
# below, not used alone.
_SESSION_COOKIE_NAMES = {"c_user", "xs"}


class SessionState(StrEnum):
    """Coarse state of the shared Facebook session.

    Mirrors the recovery flow: healthy operation, an automatic attempt in
    flight, or a human needed at the keyboard. Nothing here decides *what* to
    do about a bad state -- that is the caller's job (pause jobs, alert the
    admin, hand the browser to a human).
    """

    HEALTHY = "healthy"
    LOGIN_NEEDED = "login_needed"
    AUTO_LOGIN_ATTEMPT = "auto_login_attempt"
    HUMAN_REQUIRED = "human_required"


class FacebookSession:
    """Owns the one browser context used for every Facebook read.

    Call :meth:`start` once at process startup and keep the instance alive
    for the process lifetime; :attr:`page` hands out the single page every
    caller shares (see :attr:`lock` for why there is only one).

    Which browser gets launched is
    :attr:`FacebookSettings.browser_binary`: a path, because Playwright has
    no ``channel`` for Brave and ``executable_path`` is the only way to name
    it. The Docker image points it at Brave; unset, this falls back to
    ``channel="chrome"``, which is what a developer laptop has installed.

    Either way it is a real, installed browser rather than the
    Playwright-bundled Chromium, and the reason is worth keeping in view: the
    bundled build is what anti-bot systems fingerprint as a test browser.
    Brave is not free of that problem -- it is rarer than Chrome and
    randomises fingerprints per session unless Shields are off for
    facebook.com -- which is why the binary is configurable rather than
    compiled in. In CDP-attach mode none of this applies: the browser is
    whatever the supervisor launched (see ``docker/entrypoint.sh``) and this
    class has no control over it once attached.
    """

    def __init__(self, settings: FacebookSettings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        # True only when this instance launched the context itself and is
        # therefore responsible for closing it. False when CDP-attached to a
        # Chrome someone else supervises -- closing that context would close
        # tabs on a shared production browser, which is not ours to do.
        self._owns_context = False

    async def start(self) -> None:
        if self._context is not None:
            return
        self._playwright = await async_playwright().start()

        if self.settings.cdp_url:
            self._browser = await self._playwright.chromium.connect_over_cdp(self.settings.cdp_url)
            self._context = (
                self._browser.contexts[0]
                if self._browser.contexts
                else await self._browser.new_context()
            )
            self._owns_context = False
            log.info("facebook.session.attached_cdp", cdp_url=self.settings.cdp_url)
        else:
            profile_dir = Path(self.settings.profile_dir).expanduser().resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)
            # executable_path and channel are mutually exclusive in
            # Playwright; pass exactly one.
            browser_kwargs: dict[str, Any] = (
                {"executable_path": self.settings.browser_binary}
                if self.settings.browser_binary
                else {"channel": "chrome"}
            )
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=self.settings.headless,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                **browser_kwargs,
            )
            self._owns_context = True
            log.info(
                "facebook.session.started_persistent",
                profile_dir=str(profile_dir),
                headless=self.settings.headless,
                browser=self.settings.browser_binary or "chrome",
            )

        self._page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )
        self._page.set_default_navigation_timeout(self.settings.nav_timeout_seconds * 1000)

    async def stop(self) -> None:
        if self._context is not None and self._owns_context:
            await self._context.close()
        self._context = None
        self._page = None
        self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @property
    def lock(self) -> asyncio.Lock:
        """The single browser is shared; hold this while using :attr:`page`.

        One page, one lock: reading a group, checking a comment thread and a
        future comment-posting action all drive the same tab, and Facebook's
        own session state gets confused fast if two things navigate it at
        once (see the implementation plan's "choose one owner of the
        browser"). Human takeover during recovery is the one deliberate
        exception, and it is the caller's job to arrange, not this class's.
        """
        return self._lock

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("FacebookSession.start() was not called")
        return self._page

    async def check_state(self) -> SessionState:
        """Is the session actually authenticated right now?

        Navigates home and classifies what comes back by URL, then by
        visible challenge text, then by login-form vs. logged-in markers plus
        session cookies. Observes only -- this never fills in or clicks a
        challenge widget; that decision is deliberately left to a human, see
        the module docstring and the CAPTCHA/checkpoint discussion in the
        implementation plan.

        Cheap, and meant to run before every group job -- a session can
        expire between runs without anything else telling us. It is *not*
        yet wired to run continuously during a job's own navigation (e.g.
        mid-way through reading a group); that is future work for whatever
        module ends up running group jobs.
        """
        page = self.page
        await page.goto(FACEBOOK_HOME, wait_until="domcontentloaded")
        url = page.url.lower()

        if any(fragment in url for fragment in CHECKPOINT_PATH_FRAGMENTS):
            log.warning("facebook.session.checkpoint_url", url=page.url)
            return SessionState.HUMAN_REQUIRED

        body_text = ""
        with contextlib.suppress(Exception):
            body_text = (await page.locator("body").inner_text(timeout=5000))[:5000].lower()
        if any(signal in body_text for signal in CHALLENGE_TEXT_SIGNALS):
            log.warning("facebook.session.challenge_text", url=page.url)
            return SessionState.HUMAN_REQUIRED

        if await page.locator(LOGIN_FORM_SELECTOR).count() > 0:
            return SessionState.LOGIN_NEEDED

        assert self._context is not None  # guaranteed: self.page succeeded above
        cookies = await self._context.cookies()
        has_session_cookie = any(c["name"] in _SESSION_COOKIE_NAMES for c in cookies)
        if has_session_cookie and await page.locator(LOGGED_IN_MARKER_SELECTOR).count() > 0:
            return SessionState.HEALTHY

        # None of the above matched confidently: an unrecognised layout, not
        # a confirmed good or bad state. Treat as needing a human rather than
        # guessing.
        log.warning("facebook.session.unknown_layout", url=page.url)
        return SessionState.HUMAN_REQUIRED

    async def attempt_login(self) -> bool:
        """One automatic login attempt using the configured credentials.

        Returns whether it looks like it worked. Never retried automatically
        by this method -- the caller is responsible for falling back to a
        human after exactly one try: repeated automatic attempts do not solve
        a checkpoint, they just make the account look more like a bot.
        """
        if not self.settings.login_email or not self.settings.login_password:
            log.info("facebook.session.no_credentials_configured")
            return False

        page = self.page
        await page.goto(FACEBOOK_HOME, wait_until="domcontentloaded")
        email_field = page.locator("#email, input[name='email']")
        pass_field = page.locator("#pass, input[name='pass']")
        if await email_field.count() == 0 or await pass_field.count() == 0:
            return False

        await email_field.fill(self.settings.login_email)
        await self._human_pause()
        await pass_field.fill(self.settings.login_password.get_secret_value())
        await self._human_pause()
        await page.locator("button[name='login'], button[type='submit']").first.click()
        await page.wait_for_load_state("domcontentloaded")

        state = await self.check_state()
        log.info("facebook.session.auto_login_attempt", result=state.value)
        return state == SessionState.HEALTHY

    async def _human_pause(self) -> None:
        """A short randomised pause between actions.

        Not a defeat of any specific detection system -- just not instant,
        identical timing on every action, which is the cheapest and least
        speculative thing to get right before anything more elaborate.
        """
        low, high = self.settings.action_delay_ms
        await asyncio.sleep(random.uniform(low, high) / 1000)
