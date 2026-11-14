"""``python -m scripts.sim.run --persona <file|text> --run <id> [--step]``: one simulated run, offline.

Runs the task until it is done or waits for answers, writes ``sim/runs/<id>/result.json`` and prints how many LLM,
Apify and user answers are pending and where they lie. Exit code: 0 done, 3 waiting for answers, 4 stalled.
Every run starts from an empty database and replays everything already answered (``scripts/sim/README.md``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from . import db
from .harness import Bench, Persona

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.run", description=__doc__.splitlines()[0])
    parser.add_argument("--persona", required=True, help="a persona file (JSON/YAML) or the first message itself")
    parser.add_argument("--run", required=True, help="the run id: sim/runs/<id>/, sim/answers/<id>.json")
    parser.add_argument("--mode", choices=["real_estate", "investors", "both", "post_link"],
                        help="the mode button (default: the persona's, else real_estate)")
    parser.add_argument("--step", action="store_true",
                        help="stop at the first missing answer (default: collect every pending of the pass)")
    parser.add_argument("--sim", default=str(ROOT / "sim"), help="the bench folder (default: <repo>/sim)")
    parser.add_argument("--verbose", action="store_true", help="the bot's log on stderr")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[\w.-]{1,80}", args.run):
        parser.error("--run: letters, digits, '.', '-', '_' only")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.CRITICAL,
                        format="%(levelname)s %(name)s %(message)s")
    root = Path(args.sim).resolve()
    persona = Persona.load(args.persona, args.mode)
    url = db.url(root)
    os.chdir(root)  # the services read ``.env`` from the working folder: the bench never sees a real one
    bench = Bench(root, args.run, persona, strict=args.step, database_url=url)
    code, result = asyncio.run(bench.run())
    out = root / "runs" / args.run / "result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(summary(root, args.run, result, out))
    return code


def summary(root: Path, run: str, result: dict, out: Path) -> str:
    pending = result.get("pending", {})
    answers = root / "pending_answers" / f"{run}.json"
    waiting_answer = result.get("stop_reason", "").startswith("answer")
    lines = [
        f"run {run}: {result['status']}" + (f" ({result['stop_reason']})" if result.get("stop_reason") else ""),
        f"  pending LLM:     {len(pending.get('llm', []))}  -> {root / 'pending'}/<key>.json "
        f"(answer: {root / 'cassette'}/<key>.json)",
        f"  pending Apify:   {len(pending.get('apify', []))}  -> {root / 'pending_apify'}/<actor>__<hash>.json "
        f"(answer: {root / 'apify'}/<actor>/<hash>.json)",
        f"  pending web:     {len(pending.get('web', []))}  -> {root / 'pending_web'}/<key>.json "
        f"(answer: {root / 'web'}/<key>.json)",
        f"  pending answers: {1 if waiting_answer else 0}  -> {answers} (answer: {root / 'answers'}/{run}.json)",
    ]
    if pending.get("apify_blocked"):
        lines.append(f"  Apify not asked (after an unanswered LLM call): {len(pending['apify_blocked'])}")
    reach = sum(1 for c in result.get("reach_contacts", []) if c.get("card"))
    lines += [f"  cards: {len(result.get('cards', []))}, leads: {len(result.get('leads', []))} + reach sent {reach}, "
              f"costs: ${result.get('costs_total_usd', 0)}", f"  result: {out}"]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
