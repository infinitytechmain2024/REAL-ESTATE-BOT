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

from bot.config import Settings, get_settings
from bot.exceptions import ConfigurationError
from bot.handlers import build_router, errors
from bot.logging_conf import configure_logging, get_logger
from bot.middlewares import LoggingContextMiddleware, ThrottlingMiddleware, UserMiddleware
from bot.middlewares.throttling import SearchSlots
from bot.services.db import SupabaseRepository
from bot.services.llm import LLMManager
from bot.services.parser import PageFetcher
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
    fetcher: PageFetcher
    repo: SupabaseRepository
    pipeline: ResearchPipeline
    slots: SearchSlots

    async def aclose(self) -> None:
        """Close every service, letting each failure be logged not raised."""
        for name, closer in (
            ("llm", self.llm.aclose),
            ("stt", self.stt.aclose),
            ("search", self.search.aclose),
            ("fetcher", self.fetcher.aclose),
            ("repo", self.repo.aclose),
        ):
            try:
                await closer()
            except Exception:  # noqa: BLE001 - shutdown must complete
                log.warning("shutdown.close_failed", service=name, exc_info=True)


async def build_services(settings: Settings) -> Services:
    """Construct the service graph."""
    repo = SupabaseRepository(settings.supabase)
    await repo.connect()

    llm = LLMManager(settings.llm)
    stt = STTManager(settings.stt)
    search = SearXNGClient(settings.searxng)
    fetcher = PageFetcher(settings.parser)

    pipeline = ResearchPipeline(
        settings=settings,
        llm=llm,
        search=search,
        query_builder=QueryBuilder(settings.searxng),
        fetcher=fetcher,
        repo=repo,
    )

    return Services(
        llm=llm,
        stt=stt,
        search=search,
        fetcher=fetcher,
        repo=repo,
        pipeline=pipeline,
        slots=SearchSlots(settings.telegram.max_concurrent_searches),
    )


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
    )

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(LoggingContextMiddleware())
        observer.middleware(UserMiddleware(services.repo))

    dispatcher.message.middleware(
        ThrottlingMiddleware(settings.telegram.request_cooldown_seconds)
    )

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


async def run_polling(bot: Bot, dispatcher: Dispatcher, settings: Settings, services: Services) -> None:
    """Long polling. The default, and what Render's background worker runs."""
    await _on_startup(bot, settings, services)
    # Updates queued while the bot was down are usually stale by the time it
    # comes back; answering them confuses users more than dropping them.
    await bot.delete_webhook(drop_pending_updates=True)
    await dispatcher.start_polling(bot, handle_signals=True)


async def run_webhook(bot: Bot, dispatcher: Dispatcher, settings: Settings, services: Services) -> None:
    """Webhook mode: an aiohttp server Telegram posts updates to."""
    from aiohttp import web
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
