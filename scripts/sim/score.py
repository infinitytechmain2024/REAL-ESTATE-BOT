"""``python -m scripts.sim.score [--scores sim/scores.jsonl] [--check]``: the judge's scores by role, the goal.

Reads the judge's lines (``docs/v3/SIM_RUBRIC.md`` §6), one per run, groups them by ``batch`` (one pass of the
persona set), prints the score of every role and their mean per batch, and checks the owner's goal: the mean by roles
>= 90 % on at least 3 batches in a row, ending with the last one. ``--check``: exit code 1 until the goal is met.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TARGET = 0.9
STREAK = 3
KINDS = {"buyer_home": "objects", "buyer_land": "objects", "renter": "objects", "investor_coinvest": "leads",
         "agent_buyers": "leads", "post_link": "leads", "both": "both"}


@dataclass
class RunScore:
    """One judged run: its score in 0..1, or None (no data: excluded, the role is not confirmed)."""

    run: str
    role: str
    score: float | None
    failed: list[str] = field(default_factory=list)  # why the run scored 0 (junk host, no cards on real objects)


def _objects(line: dict[str, Any], why: list[str]) -> float | None:
    sent = int(line.get("cards_sent", 0))
    junk_hosts = line.get("junk_hosts") or []
    if junk_hosts:  # the owner's gate: 0 junk sites, whatever the cards
        why.append(f"junk hosts: {', '.join(map(str, junk_hosts))}")
        return 0.0
    if sent == 0:
        if line.get("fixture_has_objects"):
            why.append("no cards, the fixtures have objects")
            return 0.0
        return None
    return int(line.get("exact", 0)) / sent


def _leads(line: dict[str, Any], why: list[str]) -> float | None:
    sent = int(line.get("leads_sent", 0))
    if sent == 0:
        if line.get("fixture_has_leads"):
            why.append("no leads, the fixtures have them")
            return 0.0
        return None
    return int(line.get("leads_reasonable", 0)) / sent


def run_score(line: dict[str, Any]) -> RunScore:
    """The score of one line (``SIM_RUBRIC.md`` §5): objects exact/sent, leads reasonable/sent, both their mean."""
    role = str(line.get("role", ""))
    kind = KINDS.get(role)
    if kind is None:
        raise ValueError(f"run {line.get('run')!r}: unknown role {role!r}")
    why: list[str] = []
    if line.get("status") not in (None, "done"):  # the run did not reach the cards: a failure, not «no data»
        why.append(f"status {line.get('status')}")
        return RunScore(str(line.get("run")), role, 0.0, why)
    if kind == "objects":
        score = _objects(line, why)
    elif kind == "leads":
        score = _leads(line, why)
    else:
        objects, leads = _objects(line, why), _leads(line, why)
        if line.get("junk_hosts"):  # the junk gate holds for the whole run
            score = 0.0
        elif objects is None or leads is None:  # a part with no data: the role is unconfirmed, never a free 100 %
            score = None
        else:
            score = (objects + leads) / 2
    return RunScore(str(line.get("run")), role, score, why)


@dataclass
class Batch:
    name: str
    roles: dict[str, list[RunScore]] = field(default_factory=dict)

    def role_scores(self) -> dict[str, float | None]:
        """The mean of a role's runs; None when any of them has no data (the role is not confirmed)."""
        out: dict[str, float | None] = {}
        for role, runs in self.roles.items():
            values = [r.score for r in runs]
            out[role] = None if None in values else sum(v for v in values if v is not None) / len(values)
        return out

    def mean(self, expected: set[str] | None = None) -> tuple[float | None, list[str]]:
        """The mean by roles and the reasons it does not count (a missing or unconfirmed role)."""
        scores = self.role_scores()
        problems = [f"role {role}: no data" for role, value in sorted(scores.items()) if value is None]
        problems += [f"role {role}: not run" for role in sorted((expected or set()) - set(scores))]
        known = [v for v in scores.values() if v is not None]
        return (sum(known) / len(known) if known else None), problems

    def passed(self, expected: set[str] | None = None) -> bool:
        mean, problems = self.mean(expected)
        return mean is not None and not problems and mean >= TARGET


def load(path: Path) -> list[Batch]:
    """The batches in the order they first appear; a run judged twice keeps its last line."""
    batches: dict[str, dict[str, dict[str, Any]]] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: not JSON: {exc}") from exc
        for key in ("batch", "run", "role"):
            if not line.get(key):
                raise ValueError(f"{path}:{number}: no {key!r}")
        batches.setdefault(str(line["batch"]), {})[str(line["run"])] = line
    out: list[Batch] = []
    for name, runs in batches.items():
        batch = Batch(name)
        for line in runs.values():
            score = run_score(line)
            batch.roles.setdefault(score.role, []).append(score)
        out.append(batch)
    return out


def streak(batches: list[Batch], expected: set[str] | None = None) -> int:
    """How many batches in a row passed, ending with the last one."""
    count = 0
    for batch in reversed(batches):
        if not batch.passed(expected):
            break
        count += 1
    return count


def expected_roles(folder: Path) -> set[str]:
    """The roles of the persona set: a batch without one of them is incomplete. A post link the owner has not given
    yet (O7) is still expected: the goal is not met without it."""
    return {str(json.loads(p.read_text(encoding="utf-8")).get("role")) for p in sorted(folder.glob("*.json"))}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f} %"


def report(batches: list[Batch], expected: set[str] | None = None) -> str:
    roles = sorted({role for b in batches for role in b.roles} | (expected or set()))
    lines = ["| batch | " + " | ".join(roles) + " | mean | pass |", "|---" * (len(roles) + 3) + "|"]
    notes: list[str] = []
    for batch in batches:
        scores = batch.role_scores()
        mean, problems = batch.mean(expected)
        cells = [_pct(scores[r]) if r in scores else "-" for r in roles]
        lines.append(f"| {batch.name} | " + " | ".join(cells) + f" | {_pct(mean)} | "
                     f"{'yes' if batch.passed(expected) else 'no'} |")
        for runs in batch.roles.values():
            notes += [f"{batch.name}/{r.run}: {'; '.join(r.failed)}" for r in runs if r.failed]
        notes += [f"{batch.name}: {p}" for p in problems]
    run = streak(batches, expected)
    lines += ["", f"streak: {run} batch(es) in a row >= {TARGET * 100:.0f} %; goal ({STREAK} in a row): "
              f"{'met' if run >= STREAK else 'not met'}"]
    if notes:
        lines += ["", *notes]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.score", description=__doc__.splitlines()[0])
    parser.add_argument("--scores", default=str(ROOT / "sim" / "scores.jsonl"), help="the judge's lines")
    parser.add_argument("--personas", default=str(ROOT / "scripts" / "sim" / "personas"),
                        help="the persona set: its roles are expected in every batch ('' to skip the check)")
    parser.add_argument("--check", action="store_true", help="exit code 1 until the goal is met")
    args = parser.parse_args(argv)
    if not Path(args.scores).is_file():
        print(f"no scores yet: {args.scores}")
        return 1 if args.check else 0
    batches = load(Path(args.scores))
    expected = expected_roles(Path(args.personas)) if args.personas else None
    print(report(batches, expected))
    return 1 if args.check and streak(batches, expected) < STREAK else 0


if __name__ == "__main__":
    sys.exit(main())
