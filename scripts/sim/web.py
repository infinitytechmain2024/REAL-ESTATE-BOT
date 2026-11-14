"""``python -m scripts.sim.web``: answer pending SearXNG searches with the answers of earlier runs on the same topic.

The bench's search key is exact (``cassette.web_request``): a query the model words a little differently in another
run is a new pending. An answer of ``sim/web/`` carries the request it answered (``request``, copied from the
pending); a pending whose *topic* is the same — the same words of the query in any order, case and accents aside,
the same page, the same language (``es`` = ``es-ES``) and categories — gets a copy of that answer under its own key.
Engines do not count. Every other pending is left for the operator's real web search (``scripts/sim/README.md``,
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


def topic(query: str, params: dict[str, Any]) -> str:
    """The loose key of a search: the set of its words and the parameters that change the page."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", query.casefold()) if not unicodedata.combining(c))
    words = " ".join(sorted(set(re.findall(r"\w+", plain))))
    language = str(params.get("language") or "").split("-")[0].lower()
    categories = ",".join(params.get("categories") or [])
    return f"{words}|{int(params.get('pageno') or 1)}|{language}|{categories}"


def library(sim_root: Path) -> dict[str, Path]:
    """topic -> the first answer of ``sim/web/`` (by name) that says which request it answered."""
    found: dict[str, Path] = {}
    for path in sorted((sim_root / "web").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        asked = data.get("request") if isinstance(data, dict) else None
        if isinstance(asked, dict) and "query" in asked and isinstance(data.get("results"), list):
            found.setdefault(topic(str(asked["query"]), asked.get("params") or {}), path)
    return found


def answer(sim_root: Path, *, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """(answered, left) pending searches."""
    known = library(sim_root)
    answered, left = [], []
    for path in sorted((sim_root / "pending_web").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        source = known.get(topic(str(data.get("query", "")), data.get("params") or {}))
        if source is None:
            left.append(path.name)
            continue
        entry = json.loads(source.read_text(encoding="utf-8"))
        entry["request"] = {"query": data.get("query", ""), "params": data.get("params") or {}}
        entry["reused_from"] = source.name
        if not dry_run:
            target = sim_root / "web" / f"{data['key']}.json"
            target.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            path.unlink()
        answered.append(f"{path.name} <- web/{source.name}")
    return answered, left


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.web", description=__doc__.splitlines()[0])
    parser.add_argument("--sim", default=str(ROOT / "sim"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    answered, left = answer(Path(args.sim).resolve(), dry_run=args.dry_run)
    for line in answered:
        print("answered", line)
    for name in left:
        print("left    ", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
