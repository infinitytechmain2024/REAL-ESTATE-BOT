"""Each worker image contains only the bot/ packages its Dockerfile copies.

An import of a package the image does not copy passes every other test (the
whole tree is on the path there) and only fails on the VPS, where the service
then crash-loops without a word. This rebuilds each image's bot/ tree from its
COPY lines and imports the entry points in a clean interpreter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Dockerfile -> modules its containers run (CMD and compose `command:`). Healthchecks
# run their probe at import time, so they are left out.
ENTRY_POINTS = {
    "agent-reach.Dockerfile": ["bot.agent_reach.main", "bot.agent_reach.worker"],
    "analysis-pipeline.Dockerfile": ["bot.analysis_pipeline.main"],
    "browser-session.Dockerfile": ["bot.browser_session.main"],
    "campaign-runner.Dockerfile": ["bot.campaign.runner"],
    "facebook-collector.Dockerfile": ["bot.facebook_collector.main", "bot.facebook_collector.runner"],
    "scrapling-connector.Dockerfile": ["bot.scrapling_connector.main", "bot.scrapling_connector.worker"],
    "telegram-control.Dockerfile": ["bot.control_plane.main"],
    "verification.Dockerfile": ["bot.verification.main"],
}


def _copied_bot_paths(dockerfile: Path) -> list[str]:
    paths: list[str] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts[:1] == ["COPY"]:
            paths += [p for p in parts[1:-1] if p.startswith("bot/")]
    return paths


def _build_tree(dockerfile: Path, target: Path) -> None:
    for rel in _copied_bot_paths(dockerfile):
        src = ROOT / rel
        dest = target / rel
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def test_every_image_is_listed() -> None:
    assert set(ENTRY_POINTS) == {p.name for p in (ROOT / "docker").glob("*.Dockerfile")}


@pytest.mark.parametrize(("dockerfile", "module"), [(d, m) for d, mods in ENTRY_POINTS.items() for m in mods])
def test_entry_point_imports_inside_its_image(dockerfile: str, module: str, tmp_path: Path) -> None:
    _build_tree(ROOT / "docker" / dockerfile, tmp_path)
    # Run from the image tree without PYTHONPATH, so the repo's own bot/ is not importable.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    script = f"import importlib, sys; assert sys.path[0] in ('', {str(tmp_path)!r}); importlib.import_module({module!r})"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, cwd=tmp_path, env=env)
    if result.returncode != 0:
        error = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
        if "No module named 'bot" in error or "No module named \"bot" in error:
            pytest.fail(f"{module} imports a bot/ package that {dockerfile} does not copy: {error}")
        if error.startswith("ModuleNotFoundError"):
            pytest.skip(f"third-party dependency not installed here: {error}")
        pytest.fail(f"{module} failed to import in its image layout: {error}")
