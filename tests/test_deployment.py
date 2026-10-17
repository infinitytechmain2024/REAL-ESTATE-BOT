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


_SERVICE_SETTINGS = {
    "telegram": ["bot/control_plane/settings.py"],
    "campaign-runner": ["bot/campaign/settings.py", "bot/web_search/settings.py"],
    "analysis-worker": ["bot/analysis_pipeline/settings.py"],
    "analysis-pipeline": ["bot/analysis_pipeline/settings.py"],
    "reduction-worker": ["bot/agents/settings.py"],
    "agent-reach-worker": ["bot/agent_reach/settings.py"],
    "scrapling-worker": ["bot/scrapling_connector/settings.py"],
    "verification": ["bot/verification/settings.py"],
    "watchdog": ["bot/ops/settings.py"],
}


def test_compose_passes_every_setting_to_its_service() -> None:
    """A setting that .env.example documents must reach the service that reads it."""
    import re

    import yaml

    repo = Path(__file__).resolve().parent.parent
    services = yaml.safe_load((repo / "docker-compose.yml").read_text(encoding="utf-8"))["services"]

    missing: list[str] = []
    for service, files in _SERVICE_SETTINGS.items():
        spec = services[service]
        if spec.get("env_file"):
            continue
        passed = set(spec.get("environment") or {})
        for name in files:
            text = (repo / name).read_text(encoding="utf-8")
            names = set(re.findall(r'validation_alias="([A-Z0-9_]+)"', text))
            names |= set(re.findall(r'(?:_bounded_int|_bounded|_on_off|environ\.get|env\.get)\(\s*(?:env,\s*)?"([A-Z0-9_]+)"', text))
            missing += [f"{service}: {n}" for n in sorted(names - passed)]
    assert missing == [], f"settings never reach their service: {missing}"

    example: dict[str, str] = {}
    for line in (repo / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            example[match[1]] = match[2].strip()
    stale: list[str] = []
    for service, spec in services.items():
        for key, value in (spec.get("environment") or {}).items():
            match = re.fullmatch(r"\$\{[A-Z0-9_]+:-(.*)\}", str(value))
            if match and key in example and "..." not in example[key] and match[1] != example[key]:
                stale.append(f"{service}: {key} compose={match[1]!r} .env.example={example[key]!r}")
    assert stale == [], f"compose defaults differ from .env.example: {stale}"


def test_compose_has_no_duplicate_keys() -> None:
    """PyYAML's safe_load keeps the last of two equal keys; docker compose refuses the file (a merge can do this)."""
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def construct(loader: yaml.SafeLoader, node: yaml.MappingNode):
        keys = [loader.construct_object(k, deep=True) for k, _ in node.value]
        dupes = sorted({str(k) for k in keys if keys.count(k) > 1})
        assert not dupes, f"duplicate keys in docker-compose.yml: {dupes} (line {node.start_mark.line + 1})"
        return loader.construct_mapping(node, deep=True)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct)
    repo = Path(__file__).resolve().parent.parent
    assert yaml.load((repo / "docker-compose.yml").read_text(encoding="utf-8"), Loader=Strict)["services"]

    bad = "services:\n  a:\n    environment:\n      X: 1\n      X: 2\n"
    import pytest

    with pytest.raises(AssertionError, match="duplicate keys"):
        yaml.load(bad, Loader=Strict)


def test_every_service_has_healthcheck_and_restart() -> None:
    """A service that dies or hangs silently is the 24/7 failure; one-shot (profile) services are exempt."""
    import yaml

    repo = Path(__file__).resolve().parent.parent
    services = yaml.safe_load((repo / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    long_running = {name: spec for name, spec in services.items() if "restart" in spec or not spec.get("profiles")}

    no_healthcheck = [n for n, spec in long_running.items() if not (spec.get("healthcheck") or {}).get("test")]
    no_restart = [n for n, spec in long_running.items() if spec.get("restart") != "unless-stopped"]
    assert no_healthcheck == [], f"services without a healthcheck: {no_healthcheck}"
    assert no_restart == [], f"services without restart: unless-stopped: {no_restart}"


def test_compose_healthcheck_modules_exist() -> None:
    import yaml

    repo = Path(__file__).resolve().parent.parent
    services = yaml.safe_load((repo / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    missing: list[str] = []
    for name, spec in services.items():
        test = (spec.get("healthcheck") or {}).get("test") or []
        if test[:3] == ["CMD", "python", "-m"] and not (repo / (test[3].replace(".", "/") + ".py")).exists():
            missing.append(f"{name}: {test[3]}")
    assert missing == [], missing


def _script(name: str) -> str:
    repo = Path(__file__).resolve().parent.parent
    return (repo / "scripts" / name).read_text(encoding="utf-8")


def test_backup_script_is_strict_and_deletes_only_inside_backup_dir() -> None:
    import re

    text = _script("backup.sh")
    lowered = text.lower()

    assert "set -euo pipefail" in text
    assert "10240" in text and "exit 1" in text
    assert "/root/backups" in text and "BACKUP_DIR" in text and "BACKUP_KEEP_DAYS" in text
    assert "exec -T postgres" in text and "pg_dump" in text
    for forbidden in ("drop ", "truncate", "rm -rf", "rm -r "):
        assert forbidden not in lowered
    # Every delete is scoped to the backup directory or to our own temp file.
    for line in text.splitlines():
        if re.search(r"\brm\b|-delete", line) and not line.lstrip().startswith("#"):
            assert '"$backup_dir"' in line or '"$tmp"' in line, line


def test_restore_check_never_writes_to_the_working_database() -> None:
    text = _script("restore_check.sh")

    assert "set -euo pipefail" in text
    assert "real-estate-restore-check" in text and "postgres:16" in text
    assert "trap" in text and "docker rm -f" in text
    assert "--tmpfs" in text
    assert "compose" not in text
    assert "exec -T postgres" not in text
    assert "select count(*) from campaigns" in text and "OK, campaigns=" in text


def test_every_compose_service_rotates_its_logs() -> None:
    import yaml

    repo = Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((repo / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["x-logging"] == {
        "driver": "json-file",
        "options": {"max-size": "20m", "max-file": "5"},
    }
    missing = [
        n for n, spec in compose["services"].items() if spec.get("logging") != compose["x-logging"]
    ]
    assert missing == [], f"services without log rotation: {missing}"


def test_every_service_with_an_apify_token_can_reach_the_internet() -> None:
    """api.apify.com is outside the internal `backend` network: such a service must also join `egress`."""
    import yaml

    repo = Path(__file__).resolve().parent.parent
    services = yaml.safe_load((repo / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    isolated = [name for name, spec in services.items()
                if "APIFY_TOKEN" in (spec.get("environment") or {}) and "egress" not in (spec.get("networks") or [])]
    assert not isolated
