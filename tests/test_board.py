import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_v2_board.py"
spec = importlib.util.spec_from_file_location("build_v2_board", SCRIPT)
assert spec and spec.loader
board = importlib.util.module_from_spec(spec)
sys.modules["build_v2_board"] = board
spec.loader.exec_module(board)


def _plan(root: Path, plan: str, tasks: list[dict]) -> Path:
    plan_dir = root / "docs" / plan
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    (plan_dir / "board.template.html").write_text("<script>__TASKS_JSON__</script>", encoding="utf-8")
    return plan_dir


def _copy_real(root: Path, plan: str) -> Path:
    plan_dir = root / "docs" / plan
    plan_dir.mkdir(parents=True)
    for name in ("tasks.json", "board.template.html"):
        shutil.copy(ROOT / "docs" / plan / name, plan_dir / name)
    return plan_dir


@pytest.mark.parametrize("plan", ["v2", "v3"])
def test_unknown_dependency_fails(tmp_path: Path, plan: str, capsys) -> None:
    plan_dir = _plan(tmp_path, plan, [{"id": "A", "deps": ["Z"]}])
    assert board.main(["--plan", plan], root=tmp_path) == 1
    assert "A: unknown dependency Z" in capsys.readouterr().err
    assert not (plan_dir / "board.html").exists()


@pytest.mark.parametrize("plan", ["v2", "v3"])
def test_cycle_fails(tmp_path: Path, plan: str, capsys) -> None:
    plan_dir = _plan(tmp_path, plan, [{"id": "A", "deps": ["B"]}, {"id": "B", "deps": ["A"]}])
    assert board.main(["--plan", plan], root=tmp_path) == 1
    assert "cycle: A -> B -> A" in capsys.readouterr().err
    assert not (plan_dir / "board.html").exists()


def test_v2_without_flag_builds_as_before(tmp_path: Path, capsys) -> None:
    plan_dir = _copy_real(tmp_path, "v2")
    assert board.main([], root=tmp_path) == 0
    tasks = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    assert capsys.readouterr().out == f"docs/v2/board.html: {len(tasks)} tasks\n"
    built = (plan_dir / "board.html").read_text(encoding="utf-8")
    assert built == (ROOT / "docs/v2/board.html").read_text(encoding="utf-8")
    assert not (tmp_path / "docs/v3").exists()


def test_v3_flag_builds_v3_board(tmp_path: Path, capsys) -> None:
    plan_dir = _copy_real(tmp_path, "v3")
    assert board.main(["--plan", "v3"], root=tmp_path) == 0
    data = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    assert capsys.readouterr().out == f"docs/v3/board.html: {len(data['tasks'])} tasks\n"
    built = (plan_dir / "board.html").read_text(encoding="utf-8")
    assert "__TASKS_JSON__" not in built
    assert json.dumps(data, ensure_ascii=False).replace("</", "<\\/") in built


def test_unknown_plan_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        board.main(["--plan", "v9"], root=tmp_path)
    assert exc.value.code == 2
