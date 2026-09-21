#!/usr/bin/env python3
"""Guard: the vendored SearXNG snapshot must be complete in git.

This exists because it already went wrong once. The root ``.gitignore`` carried
an unanchored ``data/`` rule -- meant for the bot's own runtime directory, where
the Facebook profile lives -- and an unanchored rule matches at every depth, so
it also swallowed ``searxng/searx/data/``. Those sixteen files are a real Python
package that seventeen SearXNG modules import, and without them SearXNG does not
start at all:

    ImportError: cannot import name 'data' from 'searx'

Nothing caught it. The snapshot looked complete on the machine that vendored it,
because the files were on disk there -- they were simply never committed. Lint
passed, the byte-compile passed, and ``searxng/`` is excluded from both. The
failure only appeared on a fresh clone, which is to say on the server.

So this checks the two things that would have caught it, and neither needs the
network, a browser, or SearXNG's own dependencies:

1. The files SearXNG imports at start-up are present and tracked by git.
2. Nothing anywhere under ``searxng/`` is excluded by an ignore rule -- which
   catches the next unanchored pattern too, not just this one.

Usage:
    python scripts/check_vendor.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "searxng"

# Imported by searx.webapp, searx.engines, searx.autocomplete and others during
# start-up. Not the whole directory -- just enough that a missing package is
# caught here rather than by a failed boot.
REQUIRED = (
    "searx/data/__init__.py",
    "searx/data/core.py",
    "searx/data/currencies.json",
    "searx/data/engine_descriptions.json",
    "searx/data/engine_traits.json",
    "searx/data/external_bangs.json",
    "searx/data/external_urls.json",
    "searx/data/locales.json",
    "searx/data/osm_keys_tags.json",
    "searx/data/tracker_patterns.py",
    "searx/data/useragents.json",
    "searx/data/wikidata_units.json",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout


def main() -> int:
    failures: list[str] = []

    if not VENDOR.is_dir():
        print(f"FAIL  the vendored snapshot is missing entirely: {VENDOR}")
        return 1

    # 1. Present on disk, and actually committed.
    tracked = set(_git("ls-files", "searxng").splitlines())
    for rel in REQUIRED:
        path = f"searxng/{rel}"
        if not (VENDOR / rel).exists():
            failures.append(f"missing from the working tree: {path}")
        elif path not in tracked:
            failures.append(f"present on disk but not committed: {path}")

    if not failures:
        print(f"PASS  vendored SearXNG data present and tracked  -- {len(REQUIRED)} files")

    # 2. No ignore rule reaches into the snapshot -- which catches the next
    #    unanchored pattern, not just the one that caused this. Build artefacts
    #    are the exception: they are generated locally, are not part of any
    #    snapshot, and are supposed to be ignored everywhere.
    build_artefacts = ("__pycache__/", "*.py[cod]", ".venv/", "venv/", "*.egg-info/")

    ignored = subprocess.run(
        ["git", "check-ignore", "--stdin", "-v", "--no-index"],
        cwd=ROOT,
        input=_git("ls-files", "-oi", "--exclude-standard", "--directory", "searxng"),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    offenders = []
    for line in ignored.splitlines():
        # Format: <source>:<lineno>:<pattern>\t<path>
        rule, _, path = line.rpartition("\t")
        pattern = rule.rsplit(":", 1)[-1]
        if pattern not in build_artefacts:
            offenders.append(f"ignore rule excludes a vendored file -- {pattern} matches {path}")

    if offenders:
        failures.extend(offenders)
    else:
        print("PASS  no ignore rule reaches into searxng/")

    if failures:
        print()
        for problem in failures:
            print(f"FAIL  {problem}")
        print(
            "\nThe snapshot is incomplete, so SearXNG will not start. Restore the "
            "missing files from the commit pinned in searxng/VENDOR.md and anchor "
            "the offending .gitignore rule (`/data/`, not `data/`)."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
