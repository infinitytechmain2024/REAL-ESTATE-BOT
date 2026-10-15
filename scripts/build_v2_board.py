"""Rebuild docs/v2/board.html from docs/v2/tasks.json (the single source of truth for v2 task status).

Run after every status change: ``python3 scripts/build_v2_board.py``. Fails on a dependency that names no task or a
dependency cycle, so the board never shows a plan that cannot be scheduled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "docs/v2/tasks.json"
TEMPLATE = ROOT / "docs/v2/board.template.html"
BOARD = ROOT / "docs/v2/board.html"


def check(tasks: list[dict]) -> list[str]:
    ids = {t["id"] for t in tasks}
    errors = [f"{t['id']}: unknown dependency {d}" for t in tasks for d in t["deps"] if d not in ids]
    deps = {t["id"]: t["deps"] for t in tasks}
    state: dict[str, int] = {}

    def visit(node: str, path: tuple[str, ...]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            errors.append("cycle: " + " -> ".join((*path, node)))
            return
        state[node] = 1
        for dep in deps.get(node, []):
            visit(dep, (*path, node))
        state[node] = 2

    for node in deps:
        visit(node, ())
    return errors


def main() -> int:
    data = json.loads(TASKS.read_text(encoding="utf-8"))
    errors = check(data["tasks"])
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    BOARD.write_text(TEMPLATE.read_text(encoding="utf-8").replace("__TASKS_JSON__", payload), encoding="utf-8")
    print(f"{BOARD.relative_to(ROOT)}: {len(data['tasks'])} tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
