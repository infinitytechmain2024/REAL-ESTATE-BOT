"""T2: the persona set, the agent-user helper (``scripts/sim/answer_user.py``) and the score table
(``scripts/sim/score.py``) on synthetic data, no database, no network."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.sim import answer_user, score
from scripts.sim.harness import Persona

PERSONAS = Path(__file__).resolve().parents[2] / "scripts" / "sim" / "personas"
FILES = sorted(PERSONAS.glob("*.json"))
ROLES = set(score.KINDS)
SLOTS = {slot for slot, _ in answer_user.SLOT_PATTERNS} | {answer_user.CARD}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# -- the persona set --

def test_the_set_covers_the_golden_set_and_every_role() -> None:
    personas = [load(p) for p in FILES]
    ids = {p["id"] for p in personas}
    assert {f"G{i}" for i in range(1, 10)} <= ids
    assert len(personas) >= 12 and len(ids - {f"G{i}" for i in range(1, 10)}) >= 3
    assert {p["role"] for p in personas} == ROLES
    assert {"es", "ru", "en", "mixed"} <= {p["language"] for p in personas}
    assert any(p["voice"] for p in personas) and any(p["confirm_card"] for p in personas)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_a_persona_is_complete(path: Path) -> None:
    data = load(path)
    assert data["name"] == path.stem and data["role"] in ROLES
    persona = Persona.load(str(path))  # the T1 loader takes it as it is
    assert persona.mode in ("real_estate", "investors", "both") and persona.env == {}
    if data["role"] == "post_link":
        assert data["task_source"] == "owner_O7" and data["task"] == ""  # no link until the owner gives it
    else:
        assert data["task"] and data["task_source"] == "text"
    replies = data["replies"]
    assert set(replies) <= SLOTS, set(replies) - SLOTS
    assert any(o.get("press") == "task:launch" for o in replies["card"])
    tags = {o.get("tag") for options in replies.values() for o in options}
    if data["role"] != "post_link":  # the post link has almost no interview
        assert {"any", "contradiction"} <= tags and replies.get("clarify")
    for options in replies.values():
        for option in options:
            assert ("text" in option) != ("press" in option) and option["weight"] > 0
    kind = score.KINDS[data["role"]]
    truth = data["truth"]
    if kind in ("objects", "both"):
        hard = truth["objects"]["hard"]
        assert {"property_type", "deal", "place"} <= set(hard)
        assert all(set(c) >= {"value", "from"} for c in hard.values())
    if kind in ("leads", "both"):
        assert truth["leads"]["roles"] and truth["leads"]["not"]


def test_no_personal_data_in_the_personas() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in FILES)
    assert not re.search(r"\+?\d[\d\s-]{8,}\d", text.replace("2-3", ""))  # no phone numbers
    assert "@" not in text and "http" not in text  # no e-mails, no links (the posts come from the owner)


# -- the agent-user --

def pending(root: Path, run: str, question: str, index: int = 0, buttons: tuple[tuple[str, str], ...] = (),
            card: bool = False) -> Path:
    path = root / "pending_answers" / f"{run}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kind": "answer", "run": run, "index": index, "card": card, "question": question,
                                "buttons": [{"text": t, "data": d} for t, d in buttons], "dialogue": []},
                               ensure_ascii=False), encoding="utf-8")
    return path


SKIP = (("Не важно", "task:skip"), ("Хватит, ищи", "task:enough"), ("Отмена", "task:cancel"))


@pytest.mark.parametrize(("question", "slot"), [
    ("Какой бюджет? Например, до 1200 € или «от 500 до 900 €».", "budget"),
    ("Понял: участок в Мадриде, бюджет до 3 млн\n\nСколько комнат нужно? Например: студия, 2, от 2 до 3.", "rooms"),
    ("В каком городе искать? Напишите город, район или регион в любой стране.", "place"),
    ("Район или вся Мадрид? Например: «центр», «Руссафа и Кабаньял» или «вся Мадрид».", "district"),
    ("Если точных вариантов не найду, что допустимо? Например: бюджет +10 % (до 3 300 000 €), соседние районы, "
     "площадь от 1800 м². Или нажмите «Только точные».", "deviation"),
    ("Какой размер вложения (тикет)? Например: от 100 тыс. €, 500 тыс. – 2 млн €.", "ticket"),
    ("Вы ищете деньги для своего проекта или сами хотите вкладывать?", "investor_role"),
    ("Аренда или покупка?", "deal"),
    ("Что ищете: квартиру, дом, участок, комнату или коммерческую недвижимость?", "property_type"),
    ("Под какое строительство? Например: дом для себя. И про инвесторов: На какой стадии проект?", "details"),
    ("Какие пригороды или населённые пункты искать? Можно указать радиус в км от города.", "district"),
    ("На какой срок? Например: на год, от 6 месяцев, на лето.", "term"),
    ("Вы писали до 250 тыс. €, а теперь 400 тыс. — что верно?", "clarify"),
    ("Какая площадь, м²? Например: от 50, 60–80.", "area"),
    ("Как зовут вашу кошку?", None),
])
def test_the_slot_of_a_question(question: str, slot: str | None) -> None:
    assert answer_user.slot_of({"question": question}) == slot


def test_the_helper_answers_by_weight_with_a_seed(tmp_path: Path) -> None:
    persona = load(PERSONAS / "g1_land_madrid.json")
    picks = set()
    for seed in range(40):
        root = tmp_path / str(seed)
        pending(root, "r", "Какой бюджет? Например, до 1200 €.", buttons=SKIP)
        code, _ = answer_user.answer_pending(root, "r", persona, seed)
        assert code == answer_user.EXIT_ANSWERED
        assert not (root / "pending_answers" / "r.json").exists()
        data = load(root / "answers" / "r.json")
        picks.add(json.dumps(data["answers"][0], ensure_ascii=False))
        assert data["log"][0]["slot"] == "budget"
        # the same seed gives the same answer
        again = tmp_path / f"again{seed}"
        pending(again, "r", "Какой бюджет? Например, до 1200 €.", buttons=SKIP)
        answer_user.answer_pending(again, "r", persona, seed)
        assert load(again / "answers" / "r.json")["answers"] == data["answers"]
    options = {json.dumps({k: o[k] for k in ("text", "press") if k in o}, ensure_ascii=False)
               for o in persona["replies"]["budget"]}
    assert picks == options  # every weighted option comes up over the seeds, nothing else


def test_a_press_needs_its_button_and_a_repeat_gets_a_straight_answer(tmp_path: Path) -> None:
    persona = {"replies": {"budget": [{"press": "task:skip", "weight": 100, "tag": "any"},
                                      {"text": "до 50 тыс", "weight": 100, "tag": "contradiction"},
                                      {"text": "до 3 млн €", "weight": 1}]}}
    pending(tmp_path, "r", "Какой бюджет?", buttons=())  # no «Не важно» on screen
    answer_user.answer_pending(tmp_path, "r", persona, 0)
    first = load(tmp_path / "answers" / "r.json")["answers"][0]
    assert first.get("press") is None
    pending(tmp_path, "r", "Не понял сумму. Какой бюджет?", index=1, buttons=SKIP)
    answer_user.answer_pending(tmp_path, "r", persona, 0)
    assert load(tmp_path / "answers" / "r.json")["answers"][1] == {"text": "до 3 млн €"}


def test_the_card_and_what_is_left_pending(tmp_path: Path) -> None:
    persona = {"replies": {"card": [{"press": "task:launch", "weight": 1}], "budget": [{"text": "до 1 млн €",
                                                                                         "weight": 1}]}}
    pending(tmp_path, "r", "Проверьте задачу:\nучасток, Мадрид", card=True)
    assert answer_user.answer_pending(tmp_path, "r", persona, 1)[0] == answer_user.EXIT_ANSWERED
    assert load(tmp_path / "answers" / "r.json")["answers"] == [{"press": "task:launch"}]
    # a question the persona has no slot or reply for stays for a human: nothing is written
    for index, question in ((1, "Как зовут вашу кошку?"), (1, "Сколько комнат нужно?")):
        path = pending(tmp_path, "r", question, index=index)
        code, message = answer_user.answer_pending(tmp_path, "r", persona, 1)
        assert code == answer_user.EXIT_DEFERRED and path.exists() and question in message
    assert len(load(tmp_path / "answers" / "r.json")["answers"]) == 1
    # an old pending (answered already) is not answered twice
    pending(tmp_path, "r", "Какой бюджет?", index=0)
    assert answer_user.answer_pending(tmp_path, "r", persona, 1)[0] == answer_user.EXIT_NOTHING
    (tmp_path / "pending_answers" / "r.json").unlink()
    assert answer_user.answer_pending(tmp_path, "r", persona, 1)[0] == answer_user.EXIT_NOTHING


def test_the_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pending(tmp_path, "g1", "Какой бюджет?", buttons=SKIP)
    code = answer_user.main(["--run", "g1", "--persona", str(PERSONAS / "g1_land_madrid.json"), "--seed", "7",
                             "--sim", str(tmp_path)])
    assert code == 0 and "budget" in capsys.readouterr().out


# -- the score table --

def line(batch: str, run: str, role: str, **kw: object) -> dict:
    return {"batch": batch, "run": f"{batch}-{run}", "persona": run, "role": role, "status": "done", **kw}


def objects(batch: str, run: str, exact: int, sent: int = 10, **kw: object) -> dict:
    fields = {"cards_sent": sent, "exact": exact, "fixture_has_objects": True, "junk_hosts": [], **kw}
    return line(batch, run, str(fields.pop("role", "buyer_home")), **fields)


def leads(batch: str, run: str, good: int, sent: int = 10, role: str = "investor_coinvest") -> dict:
    return line(batch, run, role, leads_sent=sent, leads_reasonable=good, fixture_has_leads=True)


def write(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / "scores.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def good_batch(name: str) -> list[dict]:
    return [objects(name, "g2", 9), objects(name, "g1", 10, role="buyer_land"), leads(name, "g7", 9)]


def test_the_streak_of_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lines = [*good_batch("b1"), *good_batch("b2"), *good_batch("b3")]
    # b1 judged again: the last line of a run wins (8/10 -> the role at 80 %, b1 fails)
    lines.append(objects("b1", "g2", 5))
    path = write(tmp_path, lines)
    batches = score.load(path)
    assert [b.name for b in batches] == ["b1", "b2", "b3"]
    assert batches[0].role_scores()["buyer_home"] == pytest.approx(0.5)
    assert not batches[0].passed() and batches[1].passed() and score.streak(batches) == 2
    path = write(tmp_path, [*lines, *good_batch("b4")])
    assert score.streak(score.load(path)) == 3
    assert score.main(["--scores", str(path), "--personas", "", "--check"]) == 0
    out = capsys.readouterr().out
    assert "goal (3 in a row): met" in out and "| b4 |" in out
    # with the persona set every role is expected: the batches miss most of them, the goal is not met
    assert score.main(["--scores", str(path), "--check"]) == 1
    assert "not run" in capsys.readouterr().out


def test_the_mean_is_by_roles(tmp_path: Path) -> None:
    # two buyer_home runs at 100 % and one investor run at 70 %: by roles (100 + 70) / 2 = 85 %, not 90 %
    path = write(tmp_path, [objects("b", "g2", 10), objects("b", "n3", 10), leads("b", "g7", 7)])
    mean, problems = score.load(path)[0].mean()
    assert mean == pytest.approx(0.85) and not problems


def test_junk_no_data_and_failures(tmp_path: Path) -> None:
    junk = objects("b", "g2", 10, junk_hosts=["blog.example"])
    assert score.run_score(junk).score == 0.0 and "junk" in score.run_score(junk).failed[0]
    # no cards: a failure when the fixtures have objects, «no data» (not 100 %) when they have none
    empty = objects("b", "g2", 0, sent=0)
    assert score.run_score(empty).score == 0.0
    assert score.run_score({**empty, "fixture_has_objects": False}).score is None
    assert score.run_score(line("b", "g6", "post_link", leads_sent=0, fixture_has_leads=False)).score is None
    stalled = {**objects("b", "g2", 10), "status": "stalled"}
    assert score.run_score(stalled).score == 0.0
    both = line("b", "g9", "both", cards_sent=4, exact=4, junk_hosts=[], fixture_has_objects=True,
                leads_sent=4, leads_reasonable=2, fixture_has_leads=True)
    assert score.run_score(both).score == pytest.approx(0.75)
    assert score.run_score({**both, "leads_sent": 0, "fixture_has_leads": False}).score is None
    assert score.run_score({**both, "junk_hosts": ["x.example"]}).score == 0.0
    # a role with no data does not let the batch pass, even at 100 % elsewhere
    path = write(tmp_path, [objects("b", "g1", 10, role="buyer_land"), {**empty, "fixture_has_objects": False}])
    batch = score.load(path)[0]
    assert batch.role_scores()["buyer_home"] is None and not batch.passed()
    with pytest.raises(ValueError, match="unknown role"):
        score.run_score(line("b", "x", "tourist"))
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"batch": "b"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no 'run'"):
        score.load(bad)
