"""``python -m scripts.sim.answer_user --run <id> --persona <file> --seed N``: the agent-user answers by weights.

Reads ``sim/pending_answers/<run>.json``, sorts the bot's question into a slot (budget, place, area, ...), picks one of
the persona's ``replies[slot]`` with ``random.Random(f"{seed}:{run}:{index}:{slot}")`` and appends it to
``sim/answers/<run>.json``. A question no slot matches (or a slot the persona has no reply for) is left pending for
a human or agent: exit code 2, the question is printed. Protocol: ``scripts/sim/README_PERSONAS.md``.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# The slot of a question, first match wins (the order matters: the deviation question names the budget and the area,
# the generic investors' question names the sum). The bot asks in Russian; Ukrainian, es and en words for the AI
# interviewer. Stems cover the word forms the bot uses («спален/спальни», «бюджета», «районы», «площа»).
SLOT_PATTERNS: tuple[tuple[str, str], ...] = (
    # The bot noticed a contradiction («вы писали до 250 тыс., теперь 400 — что верно?»): the persona's true value.
    ("clarify", r"что верно|какой вариант верн|вы писали|ранее вы|раньше вы|правильно ли я понял|противореч|"
                r"що правильно|ви писали|раніше ви|суперечн|which is (?:it|correct)|cu[aá]l es correcto"),
    ("deviation", r"точных вариантов|точних варіант|что допустимо|що допустимо|только точные|si no hay exact|"
                  r"if no exact"),
    ("details", r"стади|под какое строительств|этаж, лифт|лифт, этаж|состояни\w* важн|важн\w* (?:ли )?состояни|"
                r"состояни[ея] (?:дома|квартиры|жилья)|строгие (?:требования|пожелания) к (?:квартире|дому)|"
                r"что планируете|под что|етап|що плануєте|etapa|stage"),
    # the land's category («suelo urbano или urbanizable/рустико», «категория земли»): before must_have, which a
    # question naming the category and the utilities together would match too. «категори» alone is not enough
    # («какая категория жилья?» is the type): only with «участ/земл/suelo» in the same question.
    ("land_status", r"urbano|urbanizable|r[uú]stic|рустик|^(?s:(?=.*категор)(?=.*(?:участ|земл|suelo|ділян)))"),
    ("investor_role", r"деньги для своего проекта|сами хотите вкладывать|ищете деньги|шукаєте гроші|"
                      r"самі хочете вкладати|raising|invertir usted"),
    ("investor_who", r"кого ищете|кого шукаєте|who are you looking|a qui[eé]n busca"),
    ("ticket", r"тикет|тікет|размер вложени|сумма вложени|сколько готовы вложить|розмір вкладен|сума вкладен|"
               r"скільки готові вкласти|ticket|cheque|inversi[oó]n m[ií]nima"),
    ("term", r"на какой срок|какой срок|когда (?:нужно|планируете|хотите)|срок аренды|на який термін|який термін|"
             r"how long|plazo|cu[aá]nto tiempo"),
    # the price per m² is about the area on every language, before ``budget``
    ("area", r"за м²|за м2|за кв\.? ?м|per square met|per m²|per m2|por m²|por m2|por metro"),
    # a bare «цена/price» is not the budget («цена включает мебель?»): only the maximum price asked for
    ("budget", r"бюджет|сколько готовы|какая цена|какую цену|до какой цены|до какой суммы|скільки готові|"
               r"яка ціна|до якої ціни|budget|max(?:imum)? price|presupuesto|precio m[aá]ximo|hasta qu[eé] precio"),
    ("property_type", r"что ищете|какой тип|тип (?:объекта|недвижимости)|квартиру, дом|що шукаєте|який тип|"
                      r"тип (?:об.єкта|нерухомості)|what type|what kind of property|qu[eé] tipo"),
    ("area", r"площ(?!адк)|м²|м2|\bm2\b|m²|square|superficie|metros"),
    ("rooms", r"комнат|спальн|спален|спалень|кімнат|bedroom|\brooms?\b|habitaci|dormitorio"),
    ("deal", r"аренда или покупка|аренду или покупку|купить или снять|снять или купить|оренда чи купівля|"
             r"купити чи орендувати|rent or buy|alquiler o compra|alquilar o comprar"),
    # «только в самом городе или можно города вокруг?» widens the place: before ``place``, which «город» matches
    ("district", r"в самом городе|города вокруг|вся провинция|окрестност|міста навколо|вся провінція|околиц"),
    ("place", r"в каком городе|где искать|в каких странах|в якому місті|де шукати|\bв яких країнах|in which city|"
              r"en qu[eé] ciudad"),
    ("district", r"район|пригород|населённые пункты|населенные пункты|передмі|населені пункти|district|"
                 r"neighbou?rhood|barrio|zona"),
    ("place", r"город|где искать|регион|стран|провинци|\bміст[оіау]|\bкраїн|провінці|\bcity\b|where|ciudad|d[oó]nde"),
    ("must_have", r"обязательно должно быть|что обязательно|коммуникац|подвед|подъезд|що обов.язково має бути|комунікац|"
                  r"під.їзд|must have|imprescindible"),
    ("sources", r"какие сайты|какие группы|які сайти|які групи|which sites|qu[eé] webs"),
)
# What to exclude («Что исключить? Например: без цены, агентства») has no slot: checked before every slot (a human
# answers it), whatever the floor, the price or the state it names.
EXCLUDE = r"исключ|виключ|skip|exclude|excluir|без цен|без ціни|sin precio"
CARD = "card"  # «Проверьте задачу»: the pending has ``card: true``
REPEAT_SKIP_TAGS = frozenset({"any", "contradiction", "low_budget", "edit"})  # asked again: only a straight answer
EXIT_ANSWERED, EXIT_NOTHING, EXIT_DEFERRED = 0, 1, 2


@dataclass
class Pick:
    slot: str | None
    answer: dict[str, Any] | None
    tag: str | None = None
    reason: str = ""


def slot_of(pending: dict[str, Any], extra: dict[str, list[str]] | None = None) -> str | None:
    """The slot of a pending question: the card, a persona's own pattern, then ``SLOT_PATTERNS``."""
    if pending.get("card"):
        return CARD
    question = str(pending.get("question", ""))
    # «Понял: …» repeats the task; the question is what follows it.
    if question.startswith("Понял:") and "\n\n" in question:
        question = question.split("\n\n", 1)[1]
    text = question.casefold()
    if re.search(EXCLUDE, text, re.IGNORECASE):
        return None
    for slot, patterns in (extra or {}).items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            return slot
    return next((slot for slot, pattern in SLOT_PATTERNS if re.search(pattern, text, re.IGNORECASE)), None)


def keyboard_label(keyboard: list[list[str]] | None, wanted: str) -> str | None:
    """The bottom-keyboard label a press names: the label itself or its words without the icon («Изменить» is
    «✏️ Изменить»); None when no such key is on screen."""
    wanted = wanted.strip()
    for row in keyboard or ():
        for label in row:
            if wanted and (wanted == label or wanted.casefold() == label.split(" ", 1)[-1].strip().casefold()):
                return label
    return None


def _button_ok(option: dict[str, Any], pending: dict[str, Any]) -> bool:
    """A press is possible only if the button is on screen, under the message or on the bottom keyboard (the card's
    «Запустить» always is)."""
    press = option.get("press")
    if press is None:
        return True
    if pending.get("card") and press in ("task:launch", "Запустить"):
        return True
    return (any(press in (b.get("text"), b.get("data")) for b in pending.get("buttons", []))
            or keyboard_label(pending.get("keyboard"), str(press)) is not None)


def choose(persona: dict[str, Any], pending: dict[str, Any], seed: int, run: str,
           history: list[dict[str, Any]] | None = None) -> Pick:
    """The persona's answer to this pending question, deterministic for (seed, run, index, slot)."""
    slot = slot_of(pending, persona.get("slot_patterns"))
    if slot is None:
        return Pick(None, None, reason="no slot matches the question")
    options = [o for o in persona.get("replies", {}).get(slot, []) if _button_ok(o, pending)]
    if not options:
        return Pick(slot, None, reason=f"the persona has no reply for slot {slot!r}")
    asked_before = any(h.get("slot") == slot for h in history or [])
    if asked_before:  # the bot asks again: it did not get it or it checks a contradiction; answer it straight
        straight = [o for o in options if o.get("tag") not in REPEAT_SKIP_TAGS]
        options = straight or options
    index = int(pending.get("index", 0))
    rng = random.Random(f"{seed}:{run}:{index}:{slot}")
    weights = [max(float(o.get("weight", 1)), 0.0) for o in options]
    if not any(weights):
        return Pick(slot, None, reason=f"every reply of slot {slot!r} has weight 0")
    option = rng.choices(options, weights=weights, k=1)[0]
    answer = {k: option[k] for k in ("text", "press") if k in option}
    return Pick(slot, answer, option.get("tag"))


def answer_pending(root: Path, run: str, persona: dict[str, Any], seed: int) -> tuple[int, str]:
    """Answer ``pending_answers/<run>.json`` into ``answers/<run>.json``; (exit code, what was done)."""
    pending_path = root / "pending_answers" / f"{run}.json"
    if not pending_path.is_file():
        return EXIT_NOTHING, f"run {run}: no pending answer"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    answers_path = root / "answers" / f"{run}.json"
    data: dict[str, Any] = {"answers": []}
    if answers_path.is_file():
        loaded = json.loads(answers_path.read_text(encoding="utf-8"))
        data = loaded if isinstance(loaded, dict) else {"answers": loaded}
        data.setdefault("answers", [])
    answers, log = data["answers"], data.setdefault("log", [])
    index = int(pending.get("index", len(answers)))
    if index < len(answers):  # an old pending: the answer is there already, the next run will ask anew
        return EXIT_NOTHING, f"run {run}: question {index} is answered already ({answers_path})"
    if index > len(answers):
        return EXIT_DEFERRED, f"run {run}: question {index} but only {len(answers)} answers: fix {answers_path}"
    pick = choose(persona, pending, seed, run, log)
    if pick.answer is None:
        return EXIT_DEFERRED, (f"run {run}: left pending ({pick.reason}): {pending.get('question', '')!r} "
                               f"buttons={[b.get('text') for b in pending.get('buttons', [])]} "
                               f"keyboard={[k for row in pending.get('keyboard') or () for k in row]}")
    answers.append(pick.answer)
    log.append({"index": index, "slot": pick.slot, "tag": pick.tag, "seed": seed})
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    answers_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending_path.unlink()
    return EXIT_ANSWERED, f"run {run}: {pick.slot} -> {pick.answer}" + (f" [{pick.tag}]" if pick.tag else "")


def answer_manually(root: Path, run: str, answer: dict[str, Any], persona: dict[str, Any] | None = None,
                    seed: int | None = None) -> tuple[int, str]:
    """A deferred question answered by hand (``--manual`` / ``--manual-press``): the answer goes in, and a ``log``
    line with ``tag: "manual"`` always goes with it (the judge sees which turns no persona weight chose)."""
    pending_path = root / "pending_answers" / f"{run}.json"
    if not pending_path.is_file():
        return EXIT_NOTHING, f"run {run}: no pending answer"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    answers_path = root / "answers" / f"{run}.json"
    data: dict[str, Any] = {"answers": []}
    if answers_path.is_file():
        loaded = json.loads(answers_path.read_text(encoding="utf-8"))
        data = loaded if isinstance(loaded, dict) else {"answers": loaded}
        data.setdefault("answers", [])
    index = int(pending.get("index", len(data["answers"])))
    if index != len(data["answers"]):
        return EXIT_DEFERRED, f"run {run}: question {index}, {len(data['answers'])} answers: fix {answers_path}"
    data["answers"].append(answer)
    data.setdefault("log", []).append({"index": index, "slot": slot_of(pending, (persona or {}).get("slot_patterns")),
                                       "tag": "manual", "seed": seed})
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    answers_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending_path.unlink()
    return EXIT_ANSWERED, f"run {run}: manual -> {answer}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.answer_user", description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="the run id (sim/pending_answers/<id>.json)")
    parser.add_argument("--persona", required=True, help="the persona JSON (scripts/sim/personas/<p>.json)")
    parser.add_argument("--seed", type=int, default=0, help="the seed of the random choice (default 0)")
    parser.add_argument("--sim", default=str(ROOT / "sim"), help="the bench folder (default: <repo>/sim)")
    manual = parser.add_mutually_exclusive_group()
    manual.add_argument("--manual", metavar="TEXT", help="answer a deferred question with this text (log tag manual)")
    manual.add_argument("--manual-press", metavar="BUTTON", help="answer it with a button (text or data)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[\w.-]{1,80}", args.run):
        parser.error("--run: letters, digits, '.', '-', '_' only")
    persona = json.loads(Path(args.persona).read_text(encoding="utf-8"))
    root = Path(args.sim).resolve()
    if args.manual is not None or args.manual_press is not None:
        answer = {"text": args.manual} if args.manual is not None else {"press": args.manual_press}
        code, message = answer_manually(root, args.run, answer, persona, args.seed)
    else:
        code, message = answer_pending(root, args.run, persona, args.seed)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
