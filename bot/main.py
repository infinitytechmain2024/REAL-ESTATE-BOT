"""Application entry point.

Builds every service once, injects them into the dispatcher's context (aiogram
passes them to handlers by parameter name), and runs either long polling or a
webhook server depending on ``TELEGRAM_MODE``.

Run with::

    python -m bot.main
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiohttp import web

from bot.config import Settings, get_settings
from bot.exceptions import ConfigurationError
from bot.handlers import build_router, errors
from bot.logging_conf import configure_logging, get_logger
from bot.middlewares import LoggingContextMiddleware, ThrottlingMiddleware, UserMiddleware
from bot.middlewares.throttling import SearchSlots
from bot.services.db import SupabaseRepository
from bot.services.facebook import FacebookSession, TokenStore, build_gate_app
from bot.services.facebook.recheck import GroupRechecker
from bot.services.facebook.watchdog import FacebookWatchdog
from bot.services.llm import LLMManager
from bot.services.parser import Fetcher, build_fetcher
from bot.services.pipeline import ResearchPipeline
from bot.services.search import QueryBuilder, SearXNGClient
from bot.services.stt import STTManager

log = get_logger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Начать и выбрать режим"),
    BotCommand(command="mode", description="Сменить режим поиска"),
    BotCommand(command="help", description="Как пользоваться ботом"),
]


@dataclass(slots=True)
class Services:
    """Everything with a lifecycle, built once and closed once."""

    llm: LLMManager
    stt: STTManager
    search: SearXNGClient
    fetcher: Fetcher
    repo: SupabaseRepository
    pipeline: ResearchPipeline
    slots: SearchSlots
    facebook_session: FacebookSession | None = None
    """None when FACEBOOK_ENABLED is false -- the admin command then says so
    rather than the bot launching a browser nobody asked for."""
    facebook_token_store: TokenStore | None = None
    """Backs the token-gated remote live-view link; None alongside facebook_session."""

    facebook_gate_runner: web.AppRunner | None = None
    """The always-on-localhost aiohttp server proxying to noVNC through a token check.
    Runs whenever Facebook is enabled, independent of whether a public tunnel is pointed
    at it -- see bot/services/facebook/gate.py."""

    facebook_watchdog: FacebookWatchdog | None = None
    facebook_watchdog_task: asyncio.Task[None] | None = None

    facebook_rechecker: GroupRechecker | None = None
    facebook_recheck_task: asyncio.Task[None] | None = None
    """Rechecks the configured group list and alerts the operator when a group
    stops being readable -- see bot/services/facebook/recheck.py."""

    async def aclose(self) -> None:
        """Close every service, letting each failure be logged not raised."""
        for task in (self.facebook_watchdog_task, self.facebook_recheck_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        closers: list[tuple[str, object]] = [
            ("llm", self.llm.aclose),
            ("stt", self.stt.aclose),
            ("search", self.search.aclose),
            ("fetcher", self.fetcher.aclose),
            ("repo", self.repo.aclose),
        ]
        if self.facebook_gate_runner is not None:
            closers.append(("facebook_gate_runner", self.facebook_gate_runner.cleanup))
        if self.facebook_session is not None:
            closers.append(("facebook_session", self.facebook_session.stop))
        for name, closer in closers:
            try:
                await closer()
            except Exception:
                log.warning("shutdown.close_failed", service=name, exc_info=True)


def deployment_warnings(settings: Settings) -> list[str]:
    """Configuration that works, but that a deployment should probably not have.

    Kept separate from validation on purpose: none of this is wrong enough to
    refuse to start, and an operator mid-incident should not be locked out of
    their own bot over a posture preference.
    """
    warnings: list[str] = []
    if not settings.facebook.enabled:
        return warnings

    if settings.facebook.login_password is not None:
        warnings.append(
            "FACEBOOK_PASSWORD is set. The human-login path is the primary one, and with "
            "2FA off on the bot account a stored password is most of what protects it -- "
            "sitting on the same machine as a browser that is already logged in. Unset "
            "both it and FACEBOOK_EMAIL unless the one automatic attempt is genuinely "
            "wanted; the live-view button covers the rest."
        )
    return warnings


async def build_services(settings: Settings, bot: Bot) -> Services:
    """Construct the service graph."""
    repo = SupabaseRepository(settings.supabase)
    await repo.connect()

    llm = LLMManager(settings.llm)
    stt = STTManager(settings.stt)
    search = SearXNGClient(settings.searxng)
    fetcher = build_fetcher(settings.parser)
    # Launches the browser now, if one is configured, so a missing
    # `playwright install` is a start-up error rather than a failed search.
    await fetcher.preflight()

    pipeline = ResearchPipeline(
        settings=settings,
        llm=llm,
        search=search,
        query_builder=QueryBuilder(settings.searxng),
        fetcher=fetcher,
        repo=repo,
    )

    # Not started here: launching a real browser is deferred to first use
    # (the /facebook admin command), so a bot run with FACEBOOK_ENABLED=true
    # but nobody touching the feature yet does not open a window for no
    # reason.
    facebook_session = FacebookSession(settings.facebook) if settings.facebook.enabled else None
    facebook_token_store = (
        TokenStore(settings.facebook.token_store_path) if settings.facebook.enabled else None
    )
    facebook_gate_runner = None
    if facebook_token_store is not None:
        facebook_gate_runner = await _start_facebook_gate(settings, facebook_token_store)

    for warning in deployment_warnings(settings):
        log.warning("startup.deployment_posture", detail=warning)

    watchdog = None
    watchdog_task = None
    rechecker = None
    recheck_task = None
    if facebook_session is not None:
        if not settings.facebook.admin_telegram_ids:
            log.warning("startup.facebook_no_admins", detail="Facebook alerts have no recipients")
        watchdog = FacebookWatchdog(facebook_session, facebook_token_store, settings, bot, repo)
        watchdog_task = asyncio.create_task(watchdog.run(), name="facebook-watchdog")
        if settings.facebook.group_urls:
            rechecker = GroupRechecker(facebook_session, settings, bot, repo)
            recheck_task = asyncio.create_task(rechecker.run(), name="facebook-group-recheck")

    return Services(
        llm=llm,
        stt=stt,
        search=search,
        fetcher=fetcher,
        repo=repo,
        pipeline=pipeline,
        slots=SearchSlots(settings.telegram.max_concurrent_searches),
        facebook_session=facebook_session,
        facebook_token_store=facebook_token_store,
        facebook_gate_runner=facebook_gate_runner,
        facebook_watchdog=watchdog,
        facebook_watchdog_task=watchdog_task,
        facebook_rechecker=rechecker,
        facebook_recheck_task=recheck_task,
    )


async def _start_facebook_gate(settings: Settings, token_store: TokenStore) -> web.AppRunner:
    """Start the token-gated proxy in front of noVNC, bound to gate_bind_address:gate_port.

    Runs whenever Facebook is enabled, regardless of whether
    FACEBOOK_DESKTOP_PUBLIC_BASE is set -- that setting only controls whether the
    Telegram alert includes a clickable link. The gate itself is harmless sitting
    unused on localhost, and building it this way means turning a public tunnel on
    or off later needs no code change here.
    """
    # The PIN cookie is the only thing separating a browser that passed the
    # PIN from one merely holding the link, so it must not travel in clear
    # once the gate is published. Loopback-only keeps it unmarked: there is no
    # network to intercept, and not every client returns a Secure cookie over
    # plain http.
    public_base = settings.facebook.desktop_public_base or ""
    app = build_gate_app(
        token_store,
        novnc_internal_url=settings.facebook.novnc_internal_url,
        pin=settings.facebook.desktop_pin,
        secure_cookie=public_base.lower().startswith("https://"),
        cookie_max_age=settings.facebook.desktop_token_ttl_seconds,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.facebook.gate_bind_address, settings.facebook.gate_port)
    await site.start()
    log.info(
        "facebook.gate.listening",
        host=settings.facebook.gate_bind_address,
        port=settings.facebook.gate_port,
    )
    return runner


def build_dispatcher(settings: Settings, services: Services) -> Dispatcher:
    """Wire routers, middlewares and the handler context."""
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        # Injected into every handler that declares a parameter of this name.
        settings=settings,
        pipeline=services.pipeline,
        repo=services.repo,
        stt=services.stt,
        llm=services.llm,
        slots=services.slots,
        facebook_session=services.facebook_session,
        facebook_token_store=services.facebook_token_store,
        facebook_watchdog=services.facebook_watchdog,
    )

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(LoggingContextMiddleware())
        observer.middleware(UserMiddleware(services.repo))

    dispatcher.message.middleware(ThrottlingMiddleware(settings.telegram.request_cooldown_seconds))

    dispatcher.include_router(build_router())
    dispatcher.include_router(errors.router)
    return dispatcher


async def _on_startup(bot: Bot, settings: Settings, services: Services) -> None:
    """Announce the bot's commands and warn about anything degraded."""
    await bot.set_my_commands(COMMANDS)

    if not await services.search.health():
        log.warning(
            "startup.searxng_unreachable",
            url=settings.searxng.url,
            detail="searches will fail until SearXNG answers; check SEARXNG_URL",
        )
    if not services.repo.enabled:
        log.warning(
            "startup.supabase_disabled",
            detail="results will not be persisted; the result buttons are hidden",
        )

    me = await bot.get_me()
    log.info(
        "startup.ready",
        bot=me.username,
        mode=settings.telegram.mode,
        llm_provider=settings.llm.provider,
        llm_model=settings.llm.model,
        stt_provider=settings.stt.provider if settings.stt.enabled else "disabled",
    )


async def run_polling(
    bot: Bot, dispatcher: Dispatcher, settings: Settings, services: Services
) -> None:
    """Long polling. The default, and what Render's background worker runs."""
    await _on_startup(bot, settings, services)
    # Updates queued while the bot was down are usually stale by the time it
    # comes back; answering them confuses users more than dropping them.
    await bot.delete_webhook(drop_pending_updates=True)
    await dispatcher.start_polling(bot, handle_signals=True)


async def run_webhook(
    bot: Bot, dispatcher: Dispatcher, settings: Settings, services: Services
) -> None:
    """Webhook mode: an aiohttp server Telegram posts updates to."""
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    telegram = settings.telegram
    assert telegram.webhook_url  # guaranteed by the settings validator

    secret = telegram.webhook_secret.get_secret_value() if telegram.webhook_secret else None
    await _on_startup(bot, settings, services)
    await bot.set_webhook(
        url=f"{telegram.webhook_url.rstrip('/')}{telegram.webhook_path}",
        secret_token=secret,
        drop_pending_updates=True,
    )

    app = web.Application()
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=secret).register(
        app, path=telegram.webhook_path
    )
    setup_application(app, dispatcher, bot=bot)

    # Render pings the service to check it is alive.
    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/healthz", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, telegram.webhook_host, telegram.webhook_port)
    await site.start()
    log.info("webhook.listening", host=telegram.webhook_host, port=telegram.webhook_port)

    try:
        await asyncio.Event().wait()  # serve until cancelled
    finally:
        await runner.cleanup()


async def main() -> None:
    """Build everything, run, and shut down cleanly."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    bot = Bot(
        token=settings.telegram.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    services = await build_services(settings, bot)
    dispatcher = build_dispatcher(settings, services)

    runner = run_webhook if settings.telegram.mode == "webhook" else run_polling
    try:
        await runner(bot, dispatcher, settings, services)
    finally:
        log.info("shutdown.started")
        await services.aclose()
        await bot.session.close()
        log.info("shutdown.complete")


def run() -> None:
    """Console entry point."""
    try:
        asyncio.run(main())
    except ConfigurationError as exc:
        # A missing key is a deployment problem, not a crash to debug.
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        run()
