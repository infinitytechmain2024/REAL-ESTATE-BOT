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
from bot.services.facebook import (
    FacebookSession,
    FacebookSource,
    TokenStore,
    build_gate_app,
)
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

    async def aclose(self) -> None:
        """Close every service, letting each failure be logged not raised."""
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
            except Exception:  # noqa: BLE001 - one bad closer must not strand the rest
                log.warning("shutdown.close_failed", service=name, exc_info=True)


async def build_services(settings: Settings) -> Services:
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

    # Built before the pipeline, which takes the group reader as one of its
    # hit sources. Not *started* here: launching a real browser is deferred to
    # first use -- the /facebook admin command, or the first search that
    # actually reads groups -- so a bot run with FACEBOOK_ENABLED=true but
    # nobody touching the feature yet does not open a window for no reason.
    facebook_session = FacebookSession(settings.facebook) if settings.facebook.enabled else None
    # Two flags because these are two decisions. FACEBOOK_ENABLED gives the
    # operator a session to log into and recover; FACEBOOK_SEARCH_ENABLED puts
    # what it reads in front of users. The second is only worth making once
    # the first has proven itself against a real group.
    facebook_source = (
        FacebookSource(settings.facebook, facebook_session)
        if facebook_session is not None and settings.facebook.search_enabled
        else None
    )

    pipeline = ResearchPipeline(
        settings=settings,
        llm=llm,
        search=search,
        query_builder=QueryBuilder(settings.searxng),
        fetcher=fetcher,
        repo=repo,
        facebook=facebook_source,
    )
    facebook_token_store = (
        TokenStore(settings.facebook.token_store_path) if settings.facebook.enabled else None
    )
    facebook_gate_runner = None
    if facebook_token_store is not None:
        facebook_gate_runner = await _start_facebook_gate(settings, facebook_token_store)

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
    )


async def _start_facebook_gate(settings: Settings, token_store: TokenStore) -> web.AppRunner:
    """Start the token-gated proxy in front of noVNC, bound to gate_bind_address:gate_port.

    Runs whenever Facebook is enabled, regardless of whether
    FACEBOOK_DESKTOP_PUBLIC_BASE is set -- that setting only controls whether the
    Telegram alert includes a clickable link. The gate itself is harmless sitting
    unused on localhost, and building it this way means turning a public tunnel on
    or off later needs no code change here.
    """
    app = build_gate_app(
        token_store,
        novnc_internal_url=settings.facebook.novnc_internal_url,
        pin=settings.facebook.desktop_pin,
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

    services = await build_services(settings)
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
