"""Deployment-shape rules that no unit test would otherwise catch."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_the_bots_own_runtime_directory_is_still_ignored() -> None:
    """The rule still has to do its actual job: keep ./data out of the repo.

    That is where the Chrome profile and the live-view token file live, both
    of which are credentials.
    """
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "check-ignore", "data/facebook_profile/Cookies"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, "./data is no longer ignored — session cookies could be committed"


def test_no_environment_file_is_tracked_except_the_example() -> None:
    """`.env.backup` reached this repository, with values in it.

    The ignore rule was `*.env`, which matches a file *ending* in .env -- not
    the shape a backup takes. The example file is the one env file that belongs
    here, because it carries names and no values.
    """
    repo = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.env", ".env", ".env.*"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    offenders = [name for name in tracked if name and name != ".env.example"]

    assert offenders == [], f"environment files are tracked in git: {offenders}"


def test_an_env_backup_cannot_be_added_again() -> None:
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "check-ignore", ".env.backup"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, ".env.backup is not gitignored"


def test_vps_update_script_migrates_restarts_and_never_drops_data() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/update.sh"
    text = script.read_text(encoding="utf-8")
    assert os.access(script, os.X_OK)
    assert "git merge --ff-only" in text and "./scripts/apply_migrations.sh" in text
    assert "pull --ignore-buildable" in text and "build --pull" in text and "up -d --force-recreate" in text
    assert text.index("apply_migrations") < text.index("up -d --force-recreate")  # schema first, then new code
    assert "down -v" not in text and "volume rm" not in text and "prune -a" not in text
