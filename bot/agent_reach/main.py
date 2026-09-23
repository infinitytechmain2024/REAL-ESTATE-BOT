"""One-shot controlled task entrypoint for a future Orchestra dispatcher."""

from __future__ import annotations

import asyncio
import json
import os

from bot.facebook_collector.browser import BrowserSessionClient

from .models import ReachPlatform, ReachSkill, ReachTask
from .runner import ControlledAgentReach
from .settings import AgentReachSettings


def _task(raw: str) -> ReachTask:
    data = json.loads(raw)
    return ReachTask(
        task_id=str(data["task_id"]), platform=ReachPlatform(data["platform"]), targets=tuple(data["targets"]),
        browser_profile_id=str(data["browser_profile_id"]), browser_profile_name=str(data["browser_profile_name"]),
        browser_profile_state=str(data.get("browser_profile_state", "ready")),
        allowed_skills=tuple(ReachSkill(skill) for skill in data.get("allowed_skills", ["read_public_page", "extract_public_text"])),
    )


async def run_task(raw: str) -> dict[str, object]:
    settings = AgentReachSettings()
    browser = BrowserSessionClient(settings.browser_url, settings.browser_token)
    runner = ControlledAgentReach(browser, max_pages=settings.max_pages, max_execution_seconds=settings.max_execution_seconds, page_timeout_seconds=settings.page_timeout_seconds)
    return (await runner.run(_task(raw))).as_dict()


if __name__ == "__main__":
    value = os.environ.get("AGENT_REACH_TASK_JSON")
    if not value:
        raise SystemExit("AGENT_REACH_TASK_JSON is required; this worker runs exactly one policy-validated task")
    print(json.dumps(asyncio.run(run_task(value)), ensure_ascii=False))
