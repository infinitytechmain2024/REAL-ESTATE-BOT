"""Intentional worker entry point; orchestration invokes batches by ID later."""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

from bot.acquisition.apify_client import ApifyHttp
from bot.utils import costs

from .browser import BrowserSessionClient
from .collector import FacebookBatchCollector
from .provider import AcquisitionProvider
from .reader import FacebookGroupReader
from .settings import FacebookCollectorSettings
from .store import PostgresCollectorStore


def choose_provider(settings: FacebookCollectorSettings, campaign_provider: str | None) -> str:
    """``apify`` only with FEATURE_APIFY_FACEBOOK on and the campaign set to it (or APIFY_FACEBOOK_SCOPE=all); else
    ``local``, the browser collector -- also for a batch that belongs to no campaign."""
    if not settings.apify_enabled or campaign_provider == "fallback":  # a closed groups' batch: always the browser
        return "local"
    if settings.apify_scope == "all":
        return "apify"
    return "apify" if campaign_provider == "apify" else "local"


async def campaign_provider(pool: asyncpg.Pool[asyncpg.Record], batch_id: str) -> str | None:
    """The ``campaigns.acquisition_provider`` of the batch's campaign (migration 046), None without a campaign;
    ``fallback`` for the browser batch of an Apify batch's closed groups."""
    value = await pool.fetchval(
        """select case when b.requested_by like 'apify_fallback:%' then 'fallback' else c.acquisition_provider end
             from acquisition_batches b left join campaigns c on c.id = b.campaign_id
            where b.id = $1::uuid""", batch_id)
    return str(value) if value else None


def build_provider(pool: asyncpg.Pool[asyncpg.Record], settings: FacebookCollectorSettings, name: str = "local", *,
                   apify_http: ApifyHttp | None = None) -> AcquisitionProvider:
    """The provider that executes a Facebook batch: the local browser collector, or the Apify adapter."""
    if name == "apify":
        config = settings.apify_config()
        if config is None:
            raise ValueError("FEATURE_APIFY_FACEBOOK is off")
        from .apify.provider import ApifyFacebookProvider
        from .apify.store import PostgresApifyFacebookStore

        http = apify_http or ApifyHttp(settings.apify_token)
        return ApifyFacebookProvider(PostgresApifyFacebookStore(pool, max_runtime_seconds=settings.apify_timeout_seconds),
                                     http, config)
    browser = BrowserSessionClient(settings.browser_url, settings.browser_token)
    reader = FacebookGroupReader(browser, max_posts=settings.max_posts_per_group, timeout_seconds=settings.group_timeout_seconds)
    return FacebookBatchCollector(
        PostgresCollectorStore(pool), browser, reader, max_posts=settings.max_posts_per_group,
        max_groups=settings.max_groups,
        item_timeout_seconds=settings.group_timeout_seconds, pause_min_seconds=settings.pause_min_seconds,
        pause_max_seconds=settings.pause_max_seconds,
    )


def install_ledger(pool: asyncpg.Pool[asyncpg.Record], settings: FacebookCollectorSettings) -> None:
    """With the Apify provider on, its runs are booked in campaign_costs against CAMPAIGN_BUDGET_USD."""
    if settings.apify_enabled:
        costs.install(costs.PostgresLedger(pool), settings.campaign_budget_usd)


async def run_batch_with(pool: asyncpg.Pool[asyncpg.Record], settings: FacebookCollectorSettings, batch_id: str, *,
                         apify_http: ApifyHttp | None = None) -> str:
    # With the flag off nothing is looked up: the browser collector runs the batch exactly as before.
    name = choose_provider(settings, await campaign_provider(pool, batch_id) if settings.apify_enabled else None)
    provider = build_provider(pool, settings, name, apify_http=apify_http)
    logging.getLogger(__name__).info("facebook.provider %s", name, extra={"batch_id": batch_id})
    try:
        return await provider.run(batch_id)
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None and apify_http is None:
            await close()


async def run_batch(batch_id: str) -> str:
    settings = FacebookCollectorSettings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    install_ledger(pool, settings)
    try:
        return await run_batch_with(pool, settings, batch_id)
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    batch = os.environ.get("FACEBOOK_BATCH_ID")
    if not batch:
        raise SystemExit("FACEBOOK_BATCH_ID is required; this worker runs exactly one queued batch")
    print(asyncio.run(run_batch(batch)))
