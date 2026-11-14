"""``python -m scripts.sim.web``: answer pending SearXNG searches with the answers of earlier runs on the same topic.

The bench's search key is exact (``cassette.web_request``): a query the model words a little differently in another
run is a new pending. An answer of ``sim/web/`` carries the request it answered (``request``, copied from the
pending); a pending whose *topic* is the same — the same tokens of the query in the same order, case and accents aside (a
number with a point or a comma is one token; ``-``, ``"`` and ``site:`` stay), the same page, the same language
(``es`` = ``es-ES``) and categories — gets a copy of that answer under its own key. Engines do not count; a pending
file that cannot be read is skipped and printed as ``broken``. Every other pending is left for the operator's real web search (``scripts/sim/README.md``,
«Ответы»). Prints what it answered and what is left. ``--dry-run``: print only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
# ``site:x``, a quote, a number with its point or comma (``1.5`` is not ``5.1``), a word with its ``-`` (a negation)
TOKEN = re.compile(r'site:[^\s"]+|"|-?\d+(?:[.,]\d+)+|-?\w+')


def topic(query: str, params: dict[str, Any]) -> str:
    """The loose key of a search: its tokens in order (case and accents folded) and the parameters that change the
    page."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", query.casefold()) if not unicodedata.combining(c))
    words = " ".join(TOKEN.findall(plain))
    language = str(params.get("language") or "").split("-")[0].lower()
    categories = ",".join(params.get("categories") or [])
    try:
        page = int(params.get("pageno") or 1)
    except (TypeError, ValueError):
        page = 1
    return f"{words}|{page}|{language}|{categories}"


def library(sim_root: Path) -> dict[str, Path]:
    """topic -> the first answer of ``sim/web/`` (by name) that says which request it answered."""
    found: dict[str, Path] = {}
    for path in sorted((sim_root / "web").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        asked = data.get("request") if isinstance(data, dict) else None
        if isinstance(asked, dict) and "query" in asked and isinstance(asked.get("params") or {}, dict) \
                and isinstance(data.get("results"), list):
            found.setdefault(topic(str(asked["query"]), asked.get("params") or {}), path)
    return found


def answer(sim_root: Path, *, dry_run: bool = False) -> tuple[list[str], list[str], list[str]]:
    """(answered, left, broken) pending searches."""
    known = library(sim_root)
    answered, left, broken = [], [], []
    for path in sorted((sim_root / "pending_web").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("key"), str) or not data["key"] \
                or not isinstance(data.get("params") or {}, dict):
            broken.append(path.name)
            continue
        source = known.get(topic(str(data.get("query", "")), data.get("params") or {}))
        if source is None:
            left.append(path.name)
            continue
        entry = json.loads(source.read_text(encoding="utf-8"))  # read and checked by ``library``
        entry["request"] = {"query": data.get("query", ""), "params": data.get("params") or {}}
        entry["reused_from"] = source.name
        if not dry_run:
            target = sim_root / "web" / f"{data['key']}.json"
            target.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            path.unlink()
        answered.append(f"{path.name} <- web/{source.name}")
    return answered, left, broken


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.web", description=__doc__.splitlines()[0])
    parser.add_argument("--sim", default=str(ROOT / "sim"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    answered, left, broken = answer(Path(args.sim).resolve(), dry_run=args.dry_run)
    for line in answered:
        print("answered", line)
    for name in left:
        print("left    ", name)
    for name in broken:
        print("broken  ", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
