"""Comments under a sent object read through Apify instead of the browser (v2 B2, ``LEADS_PROVIDER=apify``).

The reader of :class:`bot.campaign.leads.CommentLeadWorker`: no Facebook profile, no browser lease. One paid run per
post (``ApifyCommentsSource`` from B1, ``maxTotalChargeUsd`` on the launch). The budget is checked before the launch;
the run id is stored BEFORE the run is awaited, so a second tick or a restart resumes it and never pays twice; a lost
launch answer is booked at its cap and the read fails (``CommentReadFailed``). The run's cost is booked once
(``costs.record_unique``, key ``comment-read:<campaign>:<finding>``). Only reading.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from bot.acquisition.apify_client import ApifyError, LaunchUnknown, TemporaryApifyError
from bot.utils import costs

from .leads import CommentReadFailed

if TYPE_CHECKING:
    from bot.acquisition.apify_facebook_comments import ApifyCommentsSource

    from .leads import ApifyReadStore, Comment, CommentRead

log = logging.getLogger(__name__)


class ApifyCommentReader:
    def __init__(self, source: ApifyCommentsSource, store: ApifyReadStore) -> None:
        self.source, self.store = source, store

    async def aclose(self) -> None:
        await self.source.aclose()

    async def read(self, read: CommentRead) -> tuple[str | None, list[Comment]]:
        with costs.scope(read.campaign_id):  # the run is booked on the campaign
            try:
                return await self._read(read)
            except CommentReadFailed:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, ApifyError) else f"error:{type(exc).__name__}"
                await costs.error("api", code[:120], item=self.source.config.actor_id)
                if isinstance(exc, ApifyError) and not isinstance(exc, TemporaryApifyError):
                    raise CommentReadFailed(code) from None  # 400/402: a retry cannot help
                raise

    async def _estimate(self, read: CommentRead) -> None:
        cfg = self.source.config
        await costs.record_unique("api", key=f"comment-read:{read.campaign_id}:{read.finding_id}", provider="apify",
                                  item=cfg.actor_id, cost_usd=cfg.max_usd_per_post, code="estimated")

    async def _read(self, read: CommentRead) -> tuple[str | None, list[Comment]]:
        cfg = self.source.config
        run_id = await self.store.run_id(read)
        if run_id is not None and run_id.startswith("pending:"):  # a launch began, its run id was never stored
            await self._estimate(read)
            raise CommentReadFailed("launch_unconfirmed")
        if run_id is None:
            if await costs.over_budget(read.campaign_id):
                await costs.skip("api", "budget_cap", item=cfg.actor_id)
                raise CommentReadFailed("budget_cap")
            await self.store.mark_launching(read)  # before the paid launch: a failure from here on never repeats it
            try:
                run_id = await self.source.launch(read.post_url)
            except LaunchUnknown:  # a run may exist remotely: booked at its cap, never launched a second time
                await self._estimate(read)
                raise CommentReadFailed("apify_launch_unknown") from None
            try:
                saved = await self.store.save_run(read, run_id)
            except Exception:  # noqa: BLE001 - the run exists but its id is lost: never queue a second paid one
                saved = False
            if not saved:
                await self._estimate(read)
                raise CommentReadFailed("launch_unconfirmed")
        try:
            run = await self.source.wait(run_id)
        except Exception:
            # The run was paid for but never reconciled: booked at its cap (a late real usage replaces it).
            await self._estimate(read)
            raise
        usage = run.get("usageTotalUsd")
        known = isinstance(usage, int | float) and not isinstance(usage, bool) and math.isfinite(usage) and usage >= 0
        await costs.record_unique("api", key=f"comment-read:{read.campaign_id}:{read.finding_id}", provider="apify",
                                  item=cfg.actor_id, cost_usd=float(usage) if known else cfg.max_usd_per_post,
                                  code="" if known else "estimated", campaign_id=read.campaign_id)
        status = str(run.get("status"))
        if status != "SUCCEEDED":
            raise CommentReadFailed("apify_run_" + status.lower().replace("-", "_"))
        result = await self.source.comments(str(run.get("defaultDatasetId") or ""))
        if result.error:
            raise CommentReadFailed(result.error)
        return result.post_text, result.comments
