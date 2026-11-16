"""Q72: ``scripts/sim/score.py`` sets aside the personas the owner has excluded (``task_source: owner_O7``,
``excluded_by_owner``) by default; synthetic data, no database, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.sim import score

PERSONAS = Path(__file__).resolve().parents[2] / "scripts" / "sim" / "personas"
SET = {"home_a": ("buyer_home", {}), "inv_a": ("investor_coinvest", {}),
       "post_a": ("post_link", {"task_source": "owner_O7"}),
       "agent_a": ("agent_buyers", {"excluded_by_owner": "2026-11-16: no buyers in the fixtures"})}


@pytest.fixture
def persona_set(tmp_path: Path) -> Path:
    folder = tmp_path / "personas"
    folder.mkdir()
    for number, (name, (role, extra)) in enumerate(SET.items(), 1):
        data = {"id": f"X{number}", "role": role, "task_source": "text", **extra}
        (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


def batch(name: str, seed: int, good: int = 10) -> list[dict]:
    home = {"cards_sent": 10, "exact": good, "similar": 10 - good, "miss": 0, "junk": 0, "exact_available": 10,
            "available_refs": [f"f{i}" for i in range(10)], "junk_hosts": []}
    inv = {"leads_sent": 10, "leads_reasonable": good, "leads_doubtful": 10 - good, "leads_miss": 0,
           "reasonable_available": 10, "reasonable_refs": [f"l{i}" for i in range(10)]}
    agent = {**inv, "leads_reasonable": 0, "leads_doubtful": 10}  # not measurable: 0 % in the table
    return [{"batch": name, "run": f"{name}-{p}", "persona": p, "role": SET[p][0], "status": "done", "seed": seed,
             **fields} for p, fields in (("home_a", home), ("inv_a", inv), ("agent_a", agent))]


def write(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / "scores.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def test_excluded_by_owner_may_be_excluded(persona_set: Path) -> None:
    personas = score.PersonaSet.load(persona_set, "X4")
    assert personas.excluded == {"agent_a"} and personas.expected == {"home_a", "inv_a", "post_a"}
    assert personas.blocked["agent_a"] == "2026-11-16: no buyers in the fixtures"
    assert score.PersonaSet.load(persona_set, "agent_a,X3").excluded == {"agent_a", "post_a"}
    assert score.PersonaSet.load(persona_set, owner_blocked=True).excluded == {"agent_a", "post_a"}
    assert score.PersonaSet.load(persona_set).excluded == set()


def test_an_ordinary_persona_is_not_excluded(persona_set: Path, tmp_path: Path) -> None:
    for item in ("X1", "inv_a"):
        with pytest.raises(ValueError, match="only a persona set aside by the owner"):
            score.PersonaSet.load(persona_set, item)
    with pytest.raises(ValueError, match="only a persona set aside by the owner"):
        score.main(["--personas", str(persona_set), "--scores", str(tmp_path / "none.jsonl"), "--exclude", "X2"])
    (persona_set / "agent_a.json").write_text(json.dumps({"id": "X4", "role": "agent_buyers", "excluded_by_owner": " "}),
                                              encoding="utf-8")
    with pytest.raises(ValueError, match="excluded_by_owner must be a non-empty text"):
        score.PersonaSet.load(persona_set)


def test_the_default_excludes_the_owner_blocked(persona_set: Path, tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, [*batch("b1", 1), *batch("b2", 2), *batch("b3", 3)])
    args = ["--scores", str(path), "--personas", str(persona_set), "--check"]
    assert score.main(args) == 2  # met, but only conditionally: two personas set aside
    out = capsys.readouterr().out
    assert "goal (3 in a row): met conditionally (personas excluded)" in out
    assert ("исключены владельцем: X4 agent_a (2026-11-16: no buyers in the fixtures), "
            "X3 post_a (task_source owner_O7)") in out
    assert "agent_buyers" not in out.splitlines()[0] and "| b3 | 3 | 100 % | 100 % | 100 % | yes |" in out
    # --all: every persona expected, the agent's 0 % and the missing post count, not met
    assert score.main([*args, "--all"]) == 1
    out = capsys.readouterr().out
    assert "| agent_buyers |" in out.splitlines()[0] and "post_a: not run (no line)" in out
    assert "исключены владельцем" not in out
    # --exclude names only some of them: the others are expected
    assert score.main([*args, "--exclude", "X4"]) == 1
    assert "b3: post_a: not run (no line)" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        score.main([*args, "--all", "--exclude", "X4"])
    capsys.readouterr()


def test_the_real_set_sets_aside_g6_g8_n2() -> None:
    real = score.PersonaSet.load(PERSONAS, owner_blocked=True)
    assert real.excluded == {"g6_post_link", "g8_post_link_2", "n2_agent_buyers_estepona"}
    assert real.blocked["n2_agent_buyers_estepona"].startswith("2026-11-16: ")
