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


def test_a_manual_answer_is_logged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    persona_file = PERSONAS / "g1_land_madrid.json"
    pending(tmp_path, "m", "Как зовут вашу кошку?")
    assert answer_user.main(["--run", "m", "--persona", str(persona_file), "--sim", str(tmp_path)]) == 2
    assert answer_user.main(["--run", "m", "--persona", str(persona_file), "--seed", "4", "--sim", str(tmp_path),
                             "--manual", "кошки нет"]) == 0
    data = load(tmp_path / "answers" / "m.json")
    assert data["answers"] == [{"text": "кошки нет"}]
    assert data["log"] == [{"index": 0, "slot": None, "tag": "manual", "seed": 4}]
    assert not (tmp_path / "pending_answers" / "m.json").exists()
    pending(tmp_path, "m", "Аренда или покупка?", index=1, buttons=(("Покупка", "deal:sale"),))
    assert answer_user.main(["--run", "m", "--persona", str(persona_file), "--sim", str(tmp_path),
                             "--manual-press", "deal:sale"]) == 0
    data = load(tmp_path / "answers" / "m.json")
    assert data["answers"][1] == {"press": "deal:sale"} and data["log"][1]["tag"] == "manual"
    assert data["log"][1]["slot"] == "deal"
    capsys.readouterr()


# -- the score table --

SET = {"home_a": "buyer_home", "home_b": "buyer_home", "land_a": "buyer_land", "inv_a": "investor_coinvest",
       "post_a": "post_link"}


@pytest.fixture
def persona_set(tmp_path: Path) -> Path:
    folder = tmp_path / "personas"
    folder.mkdir()
    for number, (name, role) in enumerate(SET.items(), 1):
        (folder / f"{name}.json").write_text(json.dumps({"id": f"X{number}", "role": role}), encoding="utf-8")
    return folder


def line(batch: str, persona: str, **kw: object) -> dict:
    return {"batch": batch, "run": f"{batch}-{persona}", "persona": persona, "role": SET.get(persona, "both"),
            "status": "done", "seed": int(batch.lstrip("b") or 0), **kw}


def objects(batch: str, persona: str, exact: int, sent: int = 10, available: int = 10, **kw: object) -> dict:
    fields = {"cards_sent": sent, "exact": exact, "similar": sent - exact, "miss": 0, "junk": 0,
              "exact_available": available, "junk_hosts": [], **kw}
    return line(batch, persona, **fields)


def leads(batch: str, persona: str, good: int, sent: int = 10, available: int = 10) -> dict:
    return line(batch, persona, leads_sent=sent, leads_reasonable=good, leads_doubtful=sent - good, leads_miss=0,
                reasonable_available=available)


def write(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / "scores.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def good_batch(name: str) -> list[dict]:
    return [objects(name, "home_a", 9), objects(name, "home_b", 10), objects(name, "land_a", 10),
            leads(name, "inv_a", 9), leads(name, "post_a", 10)]


def test_the_streak_of_three(tmp_path: Path, persona_set: Path, capsys: pytest.CaptureFixture[str]) -> None:
    personas = score.PersonaSet.load(persona_set)
    lines = [*good_batch("b1"), *good_batch("b2"), *good_batch("b3")]
    # b1 judged again: the last line of a run wins (home_a 1/10 -> buyer_home (10 + 100) / 2 = 55 %,
    # the mean by roles (55 + 100 + 90 + 100) / 4 = 86 %: b1 fails)
    lines.append(objects("b1", "home_a", 1))
    path = write(tmp_path, lines)
    batches = score.load(path, personas)
    assert [b.name for b in batches] == ["b1", "b2", "b3"]
    assert batches[0].role_scores()["buyer_home"] == pytest.approx(0.55)
    assert not batches[0].passed(personas.expected) and batches[1].passed(personas.expected)
    assert score.streak(batches, personas.expected) == 2
    path = write(tmp_path, [*lines, *good_batch("b4")])
    args = ["--scores", str(path), "--personas", str(persona_set), "--check"]
    assert score.main(args) == 0
    out = capsys.readouterr().out
    assert "goal (3 in a row): met" in out and "| b4 | 4 |" in out


def test_every_persona_of_the_set_is_expected(tmp_path: Path, persona_set: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    # B1: only the easy personas run, the others skipped or stalled: not a pass whatever the mean
    def easy(name: str) -> list[dict]:
        return [objects(name, "home_a", 10), objects(name, "land_a", 10), leads(name, "inv_a", 10),
                leads(name, "post_a", 10)]
    path = write(tmp_path, [*easy("b1"), *easy("b2"), *easy("b3")])
    args = ["--scores", str(path), "--personas", str(persona_set), "--check"]
    assert score.main(args) == 1
    assert "home_b: not run (no line)" in capsys.readouterr().out
    stalled = [*easy("b3"), line("b3", "home_b", status="stalled")]
    path = write(tmp_path, [*good_batch("b1"), *good_batch("b2"), *stalled])
    assert score.main(["--scores", str(path), "--personas", str(persona_set), "--check"]) == 1
    assert "home_b: not run (status stalled)" in capsys.readouterr().out
    # --exclude (until O7): the excluded are named, the goal only conditional, exit code 2
    no_post = [x for x in [*good_batch("b1"), *good_batch("b2"), *good_batch("b3")] if x["persona"] != "post_a"]
    path = write(tmp_path, no_post)
    assert score.main(["--scores", str(path), "--personas", str(persona_set), "--check"]) == 1
    capsys.readouterr()
    assert score.main(["--scores", str(path), "--personas", str(persona_set), "--exclude", "X5", "--check"]) == 2
    out = capsys.readouterr().out
    assert "met conditionally" in out and "excluded: X5 post_a" in out
    with pytest.raises(ValueError, match="no such persona"):
        score.PersonaSet.load(persona_set, "G42")
    # the real set: every persona file is expected, G6/G8 can be excluded by id
    real = score.PersonaSet.load(PERSONAS, "G6,G8")
    assert real.excluded == {"g6_post_link", "g8_post_link_2"} and len(real.expected) == len(FILES) - 2


def test_the_seeds_of_a_streak_differ(tmp_path: Path, persona_set: Path) -> None:
    personas = score.PersonaSet.load(persona_set)
    same = [{**x, "seed": 7} for b in ("b1", "b2", "b3") for x in good_batch(b)]
    batches = score.load(write(tmp_path, same), personas)
    assert all(b.passed(personas.expected) for b in batches)
    assert score.streak(batches, personas.expected) == 1  # N1: the same seed three times is one batch of evidence
    missing = [{k: v for k, v in x.items() if k != "seed"} for x in good_batch("b1")]
    assert score.streak(score.load(write(tmp_path, missing), personas), personas.expected) == 0
    mixed = [*good_batch("b1"), {**objects("b1", "home_b", 10), "seed": 99}]
    with pytest.raises(ValueError, match="different seeds"):
        score.load(write(tmp_path, mixed), personas)


def test_the_floor_of_five(tmp_path: Path) -> None:
    # B2: one exact card out of one, seven exact in the fixtures: 1 / max(1, 5) = 20 %, not 100 %
    one = score.run_score(objects("b1", "home_a", 1, sent=1, available=7))
    assert one.score == pytest.approx(0.2) and "4 counted as misses" in one.failed[0]
    assert score.run_score(objects("b1", "home_a", 2, sent=2, available=2)).score == 1.0  # min(5, 2) = 2
    assert score.run_score(objects("b1", "home_a", 6, sent=8, available=20)).score == pytest.approx(0.75)
    assert score.run_score(objects("b1", "home_a", 0, sent=0, available=3)).score == 0.0
    assert score.run_score(objects("b1", "home_a", 0, sent=0, available=0)).score is None  # no data, not 100 %
    assert score.run_score(leads("b1", "inv_a", 2, sent=2, available=9)).score == pytest.approx(0.4)
    assert score.run_score(leads("b1", "post_a", 0, sent=0, available=0)).score is None


def test_the_mean_is_by_roles(tmp_path: Path) -> None:
    # two buyer_home runs at 100 % and one investor run at 70 %: by roles (100 + 70) / 2 = 85 %, not 90 %
    path = write(tmp_path, [objects("b1", "home_a", 10), objects("b1", "home_b", 10), leads("b1", "inv_a", 7)])
    mean, problems = score.load(path)[0].mean()
    assert mean == pytest.approx(0.85) and not problems


def test_junk_no_data_and_failures(tmp_path: Path, persona_set: Path) -> None:
    junk = objects("b1", "home_a", 10, junk_hosts=["blog.example"])
    assert score.run_score(junk).score == 0.0 and "junk" in score.run_score(junk).failed[0]
    # leads: a junk host goes to the diagnosis, not to the score
    assert score.run_score({**leads("b1", "inv_a", 9), "junk_hosts": ["x.example"]}).score == pytest.approx(0.9)
    stalled = line("b1", "home_a", status="stalled")
    assert score.run_score(stalled).score == 0.0 and not score.run_score(stalled).done
    both = line("b1", "g9", cards_sent=4, exact=4, similar=0, miss=0, junk=0, exact_available=4, junk_hosts=[],
                leads_sent=4, leads_reasonable=2, leads_doubtful=1, leads_miss=1, reasonable_available=4)
    assert score.run_score(both).score == pytest.approx(0.75)
    no_leads = {**both, "leads_sent": 0, "leads_reasonable": 0, "leads_doubtful": 0, "leads_miss": 0,
                "reasonable_available": 0}
    assert score.run_score(no_leads).score is None
    assert score.run_score({**both, "junk_hosts": ["x.example"]}).score == 0.0
    # a role with no data does not let the batch pass, even at 100 % elsewhere
    path = write(tmp_path, [objects("b1", "land_a", 10), objects("b1", "home_a", 0, sent=0, available=0)])
    batch = score.load(path)[0]
    assert batch.role_scores()["buyer_home"] is None and not batch.passed()


@pytest.mark.parametrize(("change", "error"), [
    ({"exact": 11, "similar": -1}, "must be an integer"),
    ({"exact": 11, "similar": 0, "cards_sent": 10}, r"exact 11 > cards_sent 10"),
    ({"similar": 0}, r"exact\+similar\+miss\+junk = 9 != cards_sent 10"),
    ({"exact_available": 3}, r"exact 9 > exact_available 3"),
    ({"cards": [{"verdict": "exact"}]}, r"1 cards listed != cards_sent 10"),
    ({"cards": [{"verdict": "exact"}] * 10}, "the verdicts of cards"),
    ({"exact": "9"}, "must be an integer"),
    ({"exact_available": None}, "must be an integer"),
])
def test_the_numbers_must_add_up(tmp_path: Path, change: dict, error: str) -> None:
    # B3: a line whose numbers do not add up is refused, not scored
    with pytest.raises(ValueError, match=error):
        score.load(write(tmp_path, [{**objects("b1", "home_a", 9), **change}]))
    cards = [{"verdict": "exact"}] * 9 + [{"verdict": "similar"}]
    assert score.load(write(tmp_path, [{**objects("b1", "home_a", 9), "cards": cards}]))


@pytest.mark.parametrize(("change", "error"), [
    ({"leads_doubtful": 0}, r"leads_reasonable\+leads_doubtful\+leads_miss = 9 != leads_sent 10"),
    ({"leads_reasonable": 11, "leads_doubtful": 0}, r"leads_reasonable 11 > leads_sent 10"),
    ({"reasonable_available": 2}, r"leads_reasonable 9 > reasonable_available 2"),
    ({"leads": []}, r"0 leads listed != leads_sent 10"),
])
def test_the_lead_numbers_must_add_up(tmp_path: Path, change: dict, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        score.load(write(tmp_path, [{**leads("b1", "inv_a", 9), **change}]))


def test_a_line_needs_its_keys_and_its_persona(tmp_path: Path, persona_set: Path) -> None:
    personas = score.PersonaSet.load(persona_set)
    for key in ("batch", "run", "persona", "role", "status"):  # N2: status is required too
        bad = {k: v for k, v in objects("b1", "home_a", 9).items() if k != key}
        with pytest.raises(ValueError, match=f"no '{key}'"):
            score.load(write(tmp_path, [bad]))
    with pytest.raises(ValueError, match="unknown role"):
        score.load(write(tmp_path, [{**objects("b1", "home_a", 9), "role": "tourist"}]))
    with pytest.raises(ValueError, match="unknown persona"):
        score.load(write(tmp_path, [objects("b1", "nobody", 9, role="buyer_home")]), personas)
    with pytest.raises(ValueError, match="has role 'buyer_home'"):
        score.load(write(tmp_path, [{**objects("b1", "home_a", 9), "role": "renter"}]), personas)
    (tmp_path / "bad.jsonl").write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not JSON"):
        score.load(tmp_path / "bad.jsonl")
