#!/usr/bin/env python3
"""Bring a VPS `.env` in line with OpenRouter transcription, without ever printing a value.

    python3 scripts/fix_env.py            # show what would change (nothing is written)
    python3 scripts/fix_env.py --apply    # back up .env, then rewrite it

What it does:

- Sets STT_PROVIDER=openrouter, STT_MODEL=openai/whisper-large-v3-turbo and
  STT_TIMEOUT_SECONDS=60; adds STT_MAX_AUDIO_BYTES / STT_MAX_AUDIO_SECONDS.
- Removes settings nothing reads any more (FASTER_WHISPER_*,
  TELEGRAM_MAX_VOICE_MB, SCRAPLING_CONNECTOR_MAX_REDIRECTS).
- Collapses a variable assigned more than once into one line. Docker Compose
  uses the LAST assignment, so that value is kept -- except for
  TELEGRAM_OPERATOR_IDS, where the last non-empty list is kept so a stray
  empty line cannot silently remove every operator.
- Checks that the values docker-compose.yml requires are present.

Only variable names are ever printed. Comments and ordering are preserved.
Standard library only, so it runs on the host without the project's venv.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

ASSIGNMENT = re.compile(r"^(?P<indent>\s*)(?P<export>export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")

FORCED = {
    "STT_PROVIDER": "openrouter",
    "STT_MODEL": "openai/whisper-large-v3-turbo",
    "STT_TIMEOUT_SECONDS": "60",
}
# Added when missing or out of the range the control plane accepts.
BOUNDED_DEFAULTS = {
    "STT_MAX_AUDIO_BYTES": ("20971520", 1, 20 * 1_048_576),
    "STT_MAX_AUDIO_SECONDS": ("300", 1, 1800),
}
OBSOLETE = {
    "FASTER_WHISPER_MODEL",
    "FASTER_WHISPER_DEVICE",
    "FASTER_WHISPER_COMPUTE_TYPE",
    "TELEGRAM_MAX_VOICE_MB",
    "SCRAPLING_CONNECTOR_MAX_REDIRECTS",
}
OBSOLETE_COMMENTS = {'# Local multilingual faster-whisper. "small" fits CPU VPS use.'}
STT_HEADER_OLD = (
    "# Registered names: openai_whisper (alias: openai), groq_whisper (alias: groq),",
    "# nvidia (alias: riva). Set STT_ENABLED=false to politely refuse voice notes.",
)
STT_HEADER_NEW = (
    "# Registered names: openrouter, openai_whisper (alias: openai), groq_whisper",
    "# (alias: groq), nvidia (alias: riva). The VPS Telegram service accepts only",
    "# openrouter, on the same OPENROUTER_API_KEY as the LLM calls.",
)
REQUIRED = (
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "DATABASE_URL",
    "REDIS_URL",
    "TELEGRAM_TOKEN",
    "BROWSER_SESSION_API_TOKEN",
    "OPENROUTER_API_KEY",
)


@dataclass
class Result:
    lines: list[str]
    changes: list[str]
    problems: list[str]


def clean(raw: str) -> str:
    """The value Compose would see: inline ` # comment` and one layer of quotes removed."""
    value = raw.strip()
    if value[:1] in {'"', "'"}:
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return not value or "..." in value or lowered.startswith(("change", "your", "replace")) or value == "***"


def fix(text: str) -> Result:
    lines = text.splitlines()
    changes: list[str] = []
    problems: list[str] = []

    parsed: list[tuple[str, str] | None] = []
    for line in lines:
        match = ASSIGNMENT.match(line)
        parsed.append((match["key"], match["value"]) if match else None)

    positions: dict[str, list[int]] = {}
    for index, item in enumerate(parsed):
        if item:
            positions.setdefault(item[0], []).append(index)

    drop: set[int] = set()
    replace: dict[int, str] = {}

    for key in sorted(OBSOLETE & positions.keys()):
        drop.update(positions[key])
        changes.append(f"removed {key} (no longer used)")

    for key, where in sorted(positions.items()):
        if key in OBSOLETE or len(where) < 2:
            continue
        keep = where[-1]
        if key == "TELEGRAM_OPERATOR_IDS":
            filled = [i for i in where if clean(parsed[i][1])]  # type: ignore[index]
            keep = filled[-1] if filled else where[-1]
        drop.update(i for i in where if i != keep)
        changes.append(f"{key} was set {len(where)} times; kept one line")

    for key, value in FORCED.items():
        where = [i for i in positions.get(key, []) if i not in drop]
        if where:
            index = where[0]
            if clean(parsed[index][1]) != value:  # type: ignore[index]
                replace[index] = f"{key}={value}"
                changes.append(f"set {key}={value}")
        else:
            changes.append(f"added {key}={value}")

    # Anchor for new STT lines: after the surviving STT_MAX_AUDIO_MB or STT_PROVIDER.
    anchor = None
    for key in ("STT_MAX_AUDIO_MB", "STT_TIMEOUT_SECONDS", "STT_PROVIDER"):
        alive = [i for i in positions.get(key, []) if i not in drop]
        if alive:
            anchor = alive[0]
            break
    additions: list[str] = [f"{k}={v}" for k, v in FORCED.items() if not [i for i in positions.get(k, []) if i not in drop]]

    for key, (default, low, high) in BOUNDED_DEFAULTS.items():
        alive = [i for i in positions.get(key, []) if i not in drop]
        if not alive:
            additions.append(f"{key}={default}")
            changes.append(f"added {key}={default}")
            continue
        raw = clean(parsed[alive[0]][1])  # type: ignore[index]
        if not (raw.isdigit() and low <= int(raw) <= high):
            replace[alive[0]] = f"{key}={default}"
            changes.append(f"{key} was not an integer in {low}..{high}; set {default}")

    out: list[str] = []
    for index, line in enumerate(lines):
        if index in drop or line.strip() in OBSOLETE_COMMENTS:
            if line.strip() in OBSOLETE_COMMENTS:
                changes.append("removed the faster-whisper comment")
            continue
        out.append(replace.get(index, line))
        if index == anchor:
            out.extend(additions)
            additions = []
    if additions:
        out.extend(["", "# Telegram voice transcription (OpenRouter)", *additions])

    joined = "\n".join(out)
    if STT_HEADER_OLD[0] in joined and STT_HEADER_OLD[1] in joined:
        joined = joined.replace("\n".join(STT_HEADER_OLD), "\n".join(STT_HEADER_NEW))
        changes.append("updated the speech-to-text section comment")
    out = joined.split("\n")

    problems.extend(check(out))
    return Result(out, changes, problems)


def check(lines: list[str]) -> list[str]:
    values: dict[str, str] = {}
    for line in lines:
        match = ASSIGNMENT.match(line)
        if match:
            values[match["key"]] = clean(match["value"])
    problems = [f"{key} is missing or still a placeholder" for key in REQUIRED if is_placeholder(values.get(key, ""))]

    operators = values.get("TELEGRAM_OPERATOR_IDS", "")
    parts = operators.replace(",", " ").split()
    if not parts:
        problems.append("TELEGRAM_OPERATOR_IDS is empty: nobody can run commands or send voice notes")
    elif not all(part.isdigit() for part in parts):
        problems.append("TELEGRAM_OPERATOR_IDS must be numeric Telegram user IDs (not @usernames)")

    database_url = values.get("DATABASE_URL", "")
    if database_url and not is_placeholder(database_url):
        url = urlsplit(database_url)
        if url.hostname != "postgres":
            problems.append("DATABASE_URL host should be `postgres` (the Compose service name)")
        password = values.get("POSTGRES_PASSWORD", "")
        if password and unquote(url.password or "") != password:
            problems.append("DATABASE_URL password does not match POSTGRES_PASSWORD")
        if values.get("POSTGRES_USER") and url.username != values["POSTGRES_USER"]:
            problems.append("DATABASE_URL user does not match POSTGRES_USER")
        if values.get("POSTGRES_DB") and url.path.lstrip("/") != values["POSTGRES_DB"]:
            problems.append("DATABASE_URL database name does not match POSTGRES_DB")

    redis_url = values.get("REDIS_URL", "")
    if redis_url and not is_placeholder(redis_url):
        url = urlsplit(redis_url)
        if url.hostname != "redis":
            problems.append("REDIS_URL host should be `redis` (the Compose service name)")
        password = values.get("REDIS_PASSWORD", "")
        if password and unquote(url.password or "") != password:
            problems.append("REDIS_URL password does not match REDIS_PASSWORD")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", type=Path)
    parser.add_argument("--apply", action="store_true", help="write the fixed file (a backup is made first)")
    args = parser.parse_args()

    path: Path = args.env_file
    if not path.is_file():
        print(f"{path} not found. Run this from /opt/real-estate-bot.", file=sys.stderr)
        return 2
    original = path.read_text(encoding="utf-8")
    result = fix(original)
    fixed = "\n".join(result.lines).rstrip("\n") + "\n"

    print("Changes:" if result.changes else "No changes needed.")
    for change in dict.fromkeys(result.changes):
        print(f"  - {change}")
    if result.problems:
        print("\nNeeds your attention (edit by hand; values are never shown):")
        for problem in result.problems:
            print(f"  ! {problem}")

    if fixed == original:
        return 1 if result.problems else 0
    if not args.apply:
        print("\nDry run: nothing written. Re-run with --apply to save.")
        return 0

    backup = path.with_name(f"{path.name}.bak.{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".env.tmp.")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(fixed)
    os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, path)
    print(f"\nSaved {path} (permissions 600). Backup: {backup}")
    return 1 if result.problems else 0


if __name__ == "__main__":
    sys.exit(main())
