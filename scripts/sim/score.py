"""``python -m scripts.sim.score [--scores sim/scores.jsonl] [--exclude G6,G8] [--check]``: scores by role, the goal.

Reads the judge's lines (``docs/v3/SIM_RUBRIC.md`` §6), one per run, checks their numbers, groups them by ``batch``
(one pass of the persona set), prints the score of every role and their mean per batch, and checks the owner's goal:
the mean by roles >= 90 % on at least 3 batches in a row with different seeds, ending with the last one. Every
persona of ``scripts/sim/personas/`` is expected in every batch; ``--exclude`` drops some (until the owner gives the
post links, O7), and the goal is then only «conditionally» met. ``--check``: exit code 0 — met, 2 — met only
conditionally (with ``--exclude``), 1 — not met.
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
MIN_CARDS = 5  # fewer sent than min(5, available) count the missing ones as misses
KINDS = {"buyer_home": "objects", "buyer_land": "objects", "renter": "objects", "investor_coinvest": "leads",
         "agent_buyers": "leads", "post_link": "leads", "both": "both"}
OBJECT_VERDICTS = ("exact", "similar", "miss", "junk")
LEAD_VERDICTS = ("reasonable", "doubtful", "miss")


@dataclass
class RunScore:
    """One judged run: its score in 0..1, or None (no data: the role is not confirmed)."""

    run: str
    persona: str
    role: str
    score: float | None
    done: bool = True
    failed: list[str] = field(default_factory=list)  # why the run scored 0 or less than it looks


def _int(line: dict[str, Any], key: str, where: str) -> int:
    value = line.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where}: {key!r} must be an integer >= 0, got {value!r}")
    return value


def _check_part(line: dict[str, Any], where: str, sent_key: str, keys: dict[str, str], good: str,
                available_key: str, listed_key: str) -> None:
    sent = _int(line, sent_key, where)
    counts = {verdict: _int(line, key, where) for verdict, key in keys.items()}
    available = _int(line, available_key, where)
    if counts[good] > sent:
        raise ValueError(f"{where}: {keys[good]} {counts[good]} > {sent_key} {sent}")
    if sum(counts.values()) != sent:
        raise ValueError(f"{where}: {'+'.join(keys.values())} = {sum(counts.values())} != {sent_key} {sent}")
    if counts[good] > available:
        raise ValueError(f"{where}: {keys[good]} {counts[good]} > {available_key} {available}")
    if listed_key in line:
        items = line[listed_key]
        if not isinstance(items, list) or len(items) != sent:
            raise ValueError(f"{where}: {len(items) if isinstance(items, list) else items!r} {listed_key} listed "
                             f"!= {sent_key} {sent}")
        listed = {verdict: sum(1 for x in items if x.get("verdict") == verdict) for verdict in keys}
        if listed != counts:
            raise ValueError(f"{where}: the verdicts of {listed_key} {listed} != the counts {counts}")


def check_numbers(line: dict[str, Any], where: str) -> None:
    """The judge's numbers must add up (``SIM_RUBRIC.md`` §6); a run that is not ``done`` has none to check."""
    if line.get("status") != "done":
        return
    kind = KINDS[line["role"]]
    if kind in ("objects", "both"):
        _check_part(line, where, "cards_sent", {v: v for v in OBJECT_VERDICTS}, "exact", "exact_available", "cards")
    if kind in ("leads", "both"):
        _check_part(line, where, "leads_sent", {v: f"leads_{v}" for v in LEAD_VERDICTS}, "reasonable",
                    "reasonable_available", "leads")


def _share(good: int, sent: int, available: int, why: list[str], what: str) -> float | None:
    """good / max(sent, min(5, available)): too few sent count as misses; nothing sent, nothing available — n/a."""
    floor = min(MIN_CARDS, available)
    denominator = max(sent, floor)
    if denominator == 0:
        return None
    if sent < floor:
        why.append(f"{sent} {what} sent, {available} good in the fixtures: {floor - sent} counted as misses")
    return good / denominator


def _objects(line: dict[str, Any], why: list[str]) -> float | None:
    junk_hosts = line.get("junk_hosts") or []
    if junk_hosts:  # the owner's gate: 0 junk sites, whatever the cards (objects and both; leads: diagnosis only)
        why.append(f"junk hosts: {', '.join(map(str, junk_hosts))}")
        return 0.0
    return _share(line["exact"], line["cards_sent"], line["exact_available"], why, "cards")


def _leads(line: dict[str, Any], why: list[str]) -> float | None:
    return _share(line["leads_reasonable"], line["leads_sent"], line["reasonable_available"], why, "leads")


def run_score(line: dict[str, Any]) -> RunScore:
    """The score of one checked line (``SIM_RUBRIC.md`` §5)."""
    role = str(line.get("role", ""))
    kind = KINDS.get(role)
    if kind is None:
        raise ValueError(f"run {line.get('run')!r}: unknown role {role!r}")
    run, persona = str(line.get("run")), str(line.get("persona"))
    why: list[str] = []
    if line.get("status") != "done":  # the run did not reach the cards: a failure, never «no data»
        return RunScore(run, persona, role, 0.0, done=False, failed=[f"status {line.get('status')}"])
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
    return RunScore(run, persona, role, score, failed=why)


@dataclass
class Batch:
    name: str
    seed: Any = None
    roles: dict[str, list[RunScore]] = field(default_factory=dict)

    def personas(self) -> set[str]:
        return {r.persona for runs in self.roles.values() for r in runs}

    def role_scores(self) -> dict[str, float | None]:
        """The mean of a role's runs; None when any of them has no data (the role is not confirmed)."""
        out: dict[str, float | None] = {}
        for role, runs in self.roles.items():
            values = [r.score for r in runs]
            out[role] = None if None in values else sum(v for v in values if v is not None) / len(values)
        return out

    def mean(self, expected: set[str] | None = None) -> tuple[float | None, list[str]]:
        """The mean by roles and the reasons the batch does not count: a role with no data, a persona stalled or
        not run at all (``expected`` — the persona names of the set)."""
        scores = self.role_scores()
        problems = [f"role {role}: no data" for role, value in sorted(scores.items()) if value is None]
        problems += [f"{r.persona}: not run ({'; '.join(r.failed)})" for runs in self.roles.values() for r in runs
                     if not r.done]
        problems += [f"{name}: not run (no line)" for name in sorted((expected or set()) - self.personas())]
        known = [v for v in scores.values() if v is not None]
        return (sum(known) / len(known) if known else None), problems

    def passed(self, expected: set[str] | None = None) -> bool:
        mean, problems = self.mean(expected)
        return mean is not None and not problems and mean >= TARGET


@dataclass
class PersonaSet:
    roles: dict[str, str]  # name -> role
    ids: dict[str, str]  # name -> id (G1, N3)
    excluded: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, folder: Path, exclude: str = "") -> PersonaSet:
        roles, ids = {}, {}
        for path in sorted(folder.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            roles[path.stem], ids[path.stem] = str(data.get("role")), str(data.get("id", path.stem))
        if not roles:
            raise ValueError(f"no personas in {folder}")
        excluded: set[str] = set()
        for item in filter(None, (x.strip() for x in exclude.split(","))):
            names = {n for n in roles if item in (n, ids[n])}
            if not names:
                raise ValueError(f"--exclude {item!r}: no such persona in {folder}")
            excluded |= names
        return cls(roles, ids, excluded)

    @property
    def expected(self) -> set[str]:
        return set(self.roles) - self.excluded


def load(path: Path, personas: PersonaSet | None = None) -> list[Batch]:
    """The batches in the order they first appear; a run judged twice keeps its last line. Every line is checked
    (``ValueError``); the lines of excluded personas are skipped."""
    batches: dict[str, dict[str, dict[str, Any]]] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        where = f"{path}:{number}"
        try:
            line = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{where}: not JSON: {exc}") from exc
        for key in ("batch", "run", "persona", "role", "status"):
            if not line.get(key):
                raise ValueError(f"{where}: no {key!r}")
        if line["role"] not in KINDS:
            raise ValueError(f"{where}: unknown role {line['role']!r}")
        if personas is not None:
            name = line["persona"]
            if name not in personas.roles:
                raise ValueError(f"{where}: unknown persona {name!r}")
            if personas.roles[name] != line["role"]:
                raise ValueError(f"{where}: persona {name!r} has role {personas.roles[name]!r}, not {line['role']!r}")
            if name in personas.excluded:
                continue
        check_numbers(line, where)
        batches.setdefault(str(line["batch"]), {})[str(line["run"])] = line
    out: list[Batch] = []
    for name, runs in batches.items():
        seeds = {json.dumps(line.get("seed")) for line in runs.values()}
        if len(seeds) > 1:
            raise ValueError(f"batch {name}: different seeds {sorted(seeds)}")
        batch = Batch(name, seed=next(iter(runs.values())).get("seed"))
        for line in runs.values():
            score = run_score(line)
            batch.roles.setdefault(score.role, []).append(score)
        out.append(batch)
    return out


def streak(batches: list[Batch], expected: set[str] | None = None) -> int:
    """How many batches in a row passed, ending with the last one, each with its own seed (a repeated or missing
    seed ends the streak: the same dialogue again proves nothing new)."""
    count, seeds = 0, set()
    for batch in reversed(batches):
        if not batch.passed(expected) or batch.seed is None or json.dumps(batch.seed) in seeds:
            break
        seeds.add(json.dumps(batch.seed))
        count += 1
    return count


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f} %"


def report(batches: list[Batch], personas: PersonaSet | None = None) -> str:
    expected = personas.expected if personas else None
    expected_roles = {personas.roles[n] for n in personas.expected} if personas else set()
    roles = sorted({role for b in batches for role in b.roles} | expected_roles)
    lines = ["| batch | seed | " + " | ".join(roles) + " | mean | pass |", "|---" * (len(roles) + 4) + "|"]
    notes: list[str] = []
    for batch in batches:
        scores = batch.role_scores()
        mean, problems = batch.mean(expected)
        cells = [_pct(scores[r]) if r in scores else "-" for r in roles]
        lines.append(f"| {batch.name} | {batch.seed} | " + " | ".join(cells) + f" | {_pct(mean)} | "
                     f"{'yes' if batch.passed(expected) else 'no'} |")
        for runs in batch.roles.values():
            notes += [f"{batch.name}/{r.run}: {'; '.join(r.failed)}" for r in runs if r.failed and r.done]
        notes += [f"{batch.name}: {p}" for p in problems]
    run = streak(batches, expected)
    goal = "not met"
    if run >= STREAK:
        goal = "met conditionally (personas excluded)" if personas and personas.excluded else "met"
    lines += ["", f"streak: {run} batch(es) in a row >= {TARGET * 100:.0f} % with different seeds; "
              f"goal ({STREAK} in a row): {goal}"]
    if personas and personas.excluded:
        lines.append("excluded: " + ", ".join(f"{personas.ids[n]} {n}" for n in sorted(personas.excluded)))
    if notes:
        lines += ["", *notes]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.score", description=__doc__.splitlines()[0])
    parser.add_argument("--scores", default=str(ROOT / "sim" / "scores.jsonl"), help="the judge's lines")
    parser.add_argument("--personas", default=str(ROOT / "scripts" / "sim" / "personas"),
                        help="the persona set: every persona in it is expected in every batch")
    parser.add_argument("--exclude", default="", help="persona ids or names not expected yet, e.g. G6,G8 (until O7)")
    parser.add_argument("--check", action="store_true",
                        help="exit code 0 met, 2 met only conditionally (--exclude), 1 not met")
    args = parser.parse_args(argv)
    personas = PersonaSet.load(Path(args.personas), args.exclude)
    if not Path(args.scores).is_file():
        print(f"no scores yet: {args.scores}")
        return 1 if args.check else 0
    batches = load(Path(args.scores), personas)
    print(report(batches, personas))
    if not args.check:
        return 0
    if streak(batches, personas.expected) < STREAK:
        return 1
    return 2 if personas.excluded else 0


if __name__ == "__main__":
    sys.exit(main())
