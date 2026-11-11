"""The money of the quality cycle (T3): every live Apify run made for the bench, in ``sim/spend.jsonl``.

The bench itself never pays (its runs come from files); the orchestrator records each run it makes through MCP with
``record_spend`` or ``python -m scripts.sim.spend add``. ``python -m scripts.sim.spend`` prints the total and by
actor and exits 1 when the total is over the ceiling (``--limit``, default $10: the owner's budget for the cycle).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "sim"
LIMIT_USD = 10.0


def record_spend(actor: str, run_id: str, usd: float, note: str = "", *, sim_root: Path = SIM) -> dict[str, Any]:
    """Append one paid run: the actor, Apify's run id, its cost in USD (``usageTotalUsd``) and a note."""
    if not actor or not run_id:
        raise ValueError("actor and run_id are required")
    if isinstance(usd, bool) or not isinstance(usd, int | float) or not math.isfinite(usd) or usd < 0:
        raise ValueError("usd must be a finite non-negative number")
    entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "actor": actor, "run_id": run_id,
             "usd": float(usd), "note": note}
    sim_root.mkdir(parents=True, exist_ok=True)
    with (sim_root / "spend.jsonl").open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def entries(sim_root: Path = SIM) -> list[dict[str, Any]]:
    path = sim_root / "spend.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def total(sim_root: Path = SIM) -> float:
    return round(sum(float(e.get("usd", 0)) for e in entries(sim_root)), 6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.spend", description=__doc__.splitlines()[0])
    parser.add_argument("--sim", default=str(SIM), help="the bench folder (default: <repo>/sim)")
    parser.add_argument("--limit", type=float, default=LIMIT_USD, help="the ceiling in USD (default 10)")
    sub = parser.add_subparsers(dest="action")
    add = sub.add_parser("add", help="record one paid run")
    add.add_argument("--actor", required=True)
    add.add_argument("--run-id", required=True)
    add.add_argument("--usd", type=float, required=True)
    add.add_argument("--note", default="")
    args = parser.parse_args(argv)
    root = Path(args.sim).resolve()
    if args.action == "add":
        record_spend(args.actor, args.run_id, args.usd, args.note, sim_root=root)
    by_actor: dict[str, float] = {}
    for entry in entries(root):
        by_actor[entry["actor"]] = by_actor.get(entry["actor"], 0.0) + float(entry["usd"])
    spent = total(root)
    for actor, usd in sorted(by_actor.items()):
        print(f"  {actor}: ${usd:.4f}")
    print(f"total: ${spent:.4f} of ${args.limit:.2f} ({len(entries(root))} runs)")
    if spent > args.limit:
        print("OVER THE CEILING: no more live runs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
