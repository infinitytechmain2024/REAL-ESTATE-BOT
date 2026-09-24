"""scripts/fix_env.py: repairs a real-world VPS .env and never prints a value."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/fix_env.py"
spec = importlib.util.spec_from_file_location("fix_env", SCRIPT)
assert spec and spec.loader
fix_env = importlib.util.module_from_spec(spec)
sys.modules["fix_env"] = fix_env
spec.loader.exec_module(fix_env)

SECRETS = ("pg-secret-1", "redis-secret-2", "123:tg-secret-3", "browser-secret-4", "sk-or-v1-secret5")

# The layout of an operator's .env from the field, with fake values: duplicated
# STT and operator keys, leftover faster-whisper settings.
BROKEN = f"""\
POSTGRES_DB=monitoring
POSTGRES_USER=monitoring_app
POSTGRES_PASSWORD={SECRETS[0]}
DATABASE_URL=postgresql://monitoring_app:{SECRETS[0]}@postgres:5432/monitoring
REDIS_PASSWORD={SECRETS[1]}
REDIS_URL=redis://:{SECRETS[1]}@redis:6379/0
TELEGRAM_TOKEN={SECRETS[2]}
TELEGRAM_OPERATOR_IDS=111222333
TELEGRAM_CONFIRMATION_TTL_SECONDS=300
TELEGRAM_MAX_VOICE_MB=20
TELEGRAM_OPERATOR_IDS=
BROWSER_SESSION_API_TOKEN={SECRETS[3]}

# Local multilingual faster-whisper. "small" fits CPU VPS use.
FASTER_WHISPER_MODEL=small
FASTER_WHISPER_DEVICE=cpu
FASTER_WHISPER_COMPUTE_TYPE=int8

LLM_PROVIDER=openrouter
LLM_MODEL=some/model

STT_PROVIDER=openrouter
STT_MODEL=openai/whisper-large-v3-turbo
STT_MAX_AUDIO_BYTES=20971520
STT_TIMEOUT_SECONDS=60

OPENROUTER_API_KEY={SECRETS[4]}

# =============================================================================
# Speech-to-text (voice messages)
#
# Registered names: openai_whisper (alias: openai), groq_whisper (alias: groq),
# nvidia (alias: riva). Set STT_ENABLED=false to politely refuse voice notes.
# =============================================================================
STT_ENABLED=true
STT_PROVIDER=groq_whisper
STT_MODEL=whisper-large-v3-turbo
# STT_API_KEY=...            # falls back to GROQ_API_KEY / OPENAI_API_KEY etc.
STT_TIMEOUT_SECONDS=120
STT_MAX_AUDIO_MB=20
# --- OpenAI Whisper ----------------------------------------------------------
# STT_PROVIDER=openai_whisper
FACEBOOK_MAX_SEARCH_TERMS=3
FACEBOOK_MAX_SEARCH_TERMS=5
SCRAPLING_CONNECTOR_MAX_REDIRECTS=3
"""


def assignments(lines: list[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in lines:
        match = fix_env.ASSIGNMENT.match(line)
        if match:
            found.setdefault(match["key"], []).append(fix_env.clean(match["value"]))
    return found


def test_the_field_env_is_repaired() -> None:
    result = fix_env.fix(BROKEN)
    values = assignments(result.lines)

    assert all(len(v) == 1 for v in values.values()), {k: len(v) for k, v in values.items() if len(v) > 1}
    assert values["STT_PROVIDER"] == ["openrouter"]
    assert values["STT_MODEL"] == ["openai/whisper-large-v3-turbo"]
    assert values["STT_TIMEOUT_SECONDS"] == ["60"]
    assert values["STT_MAX_AUDIO_BYTES"] == ["20971520"]
    assert values["STT_MAX_AUDIO_SECONDS"] == ["300"]
    # The non-empty operator list survives a later empty assignment.
    assert values["TELEGRAM_OPERATOR_IDS"] == ["111222333"]
    # Otherwise the effective (last) value is kept, as Compose would read it.
    assert values["FACEBOOK_MAX_SEARCH_TERMS"] == ["5"]
    for gone in ("FASTER_WHISPER_MODEL", "FASTER_WHISPER_DEVICE", "FASTER_WHISPER_COMPUTE_TYPE", "TELEGRAM_MAX_VOICE_MB", "SCRAPLING_CONNECTOR_MAX_REDIRECTS"):
        assert gone not in values
    text = "\n".join(result.lines)
    assert "faster-whisper. \"small\"" not in text
    assert "Registered names: openrouter" in text
    # Comments, including commented-out alternatives, are untouched.
    assert "# STT_PROVIDER=openai_whisper" in text
    assert result.problems == []


def test_second_run_changes_nothing() -> None:
    once = "\n".join(fix_env.fix(BROKEN).lines)
    assert "\n".join(fix_env.fix(once).lines) == once


def test_problems_are_reported_by_name_only() -> None:
    env = BROKEN.replace(f"OPENROUTER_API_KEY={SECRETS[4]}", "OPENROUTER_API_KEY=sk-or-v1-...")
    env = env.replace("TELEGRAM_OPERATOR_IDS=111222333", "TELEGRAM_OPERATOR_IDS=@owner")
    env = env.replace("@postgres:5432", "@localhost:5432")
    problems = fix_env.fix(env).problems
    assert any("OPENROUTER_API_KEY" in p for p in problems)
    assert any("numeric" in p for p in problems)
    assert any("host should be `postgres`" in p for p in problems)


def test_mismatched_database_password_is_caught() -> None:
    env = BROKEN.replace(f"POSTGRES_PASSWORD={SECRETS[0]}", "POSTGRES_PASSWORD=other")
    assert any("does not match POSTGRES_PASSWORD" in p for p in fix_env.fix(env).problems)


def test_cli_dry_run_then_apply_never_prints_secrets(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(BROKEN, encoding="utf-8")

    dry = subprocess.run([sys.executable, str(SCRIPT), "--env-file", str(env)], capture_output=True, text=True, check=False)
    assert dry.returncode == 0, dry.stderr
    assert "Dry run" in dry.stdout
    assert env.read_text(encoding="utf-8") == BROKEN

    applied = subprocess.run([sys.executable, str(SCRIPT), "--env-file", str(env), "--apply"], capture_output=True, text=True, check=False)
    assert applied.returncode == 0, applied.stderr
    for secret in SECRETS:
        assert secret not in dry.stdout + dry.stderr + applied.stdout + applied.stderr
    backups = list(tmp_path.glob(".env.bak.*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == BROKEN
    assert (env.stat().st_mode & 0o777) == 0o600
    assert "STT_PROVIDER=groq_whisper" not in env.read_text(encoding="utf-8")
