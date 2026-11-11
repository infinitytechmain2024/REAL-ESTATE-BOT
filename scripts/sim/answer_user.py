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
# the generic investors' question names the sum). The bot asks in Russian; a few es/en words for the AI interviewer.
SLOT_PATTERNS: tuple[tuple[str, str], ...] = (
    # The bot noticed a contradiction («вы писали до 250 тыс., теперь 400 — что верно?»): the persona's true value.
    ("clarify", r"что верно|какой вариант верн|вы писали|ранее вы|раньше вы|правильно ли я понял|противореч|"
                r"which is (?:it|correct)|cu[aá]l es correcto"),
    ("deviation", r"точных вариантов|что допустимо|только точные|si no hay exact|if no exact"),
    ("details", r"стади|под какое строительств|этаж, лифт|состояние важн|etapa|stage"),
    ("investor_role", r"деньги для своего проекта|сами хотите вкладывать|ищете деньги|raising|invertir usted"),
    ("investor_who", r"кого ищете|who are you looking|a qui[eé]n busca"),
    ("ticket", r"тикет|размер вложени|сумма вложени|сколько готовы вложить|ticket|cheque|inversi[oó]n m[ií]nima"),
    ("term", r"на какой срок|какой срок|когда (?:нужно|планируете|хотите)|срок аренды|how long|plazo|cu[aá]nto tiempo"),
    ("budget", r"бюджет|сколько готовы|какая цена|до какой суммы|budget|presupuesto|precio"),
    ("property_type", r"что ищете|какой тип|тип (?:объекта|недвижимости)|квартиру, дом|what type|qu[eé] tipo"),
    ("area", r"площад|м²|м2|\bm2\b|square|superficie|metros"),
    ("rooms", r"комнат|спальн|bedroom|habitaciones|dormitorios"),
    ("deal", r"аренда или покупка|купить или снять|снять или купить|rent or buy|alquiler o compra"),
    ("place", r"в каком городе|где искать|в каких странах|in which city|en qu[eé] ciudad"),
    ("district", r"район|пригород|населённые пункты|населенные пункты|district|barrio|zona"),
    ("place", r"город|где искать|регион|стран|провинци|\bcity\b|where|ciudad|d[oó]nde"),
    ("must_have", r"обязательно должно быть|что обязательно|must have|imprescindible"),
    ("sources", r"какие сайты|какие группы|which sites|qu[eé] webs"),
)
CARD = "card"  # «Проверьте задачу»: the pending has ``card: true``
REPEAT_SKIP_TAGS = frozenset({"any", "contradiction", "edit"})  # asked again: only a straight answer
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
    for slot, patterns in (extra or {}).items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            return slot
    return next((slot for slot, pattern in SLOT_PATTERNS if re.search(pattern, text, re.IGNORECASE)), None)


def _button_ok(option: dict[str, Any], pending: dict[str, Any]) -> bool:
    """A press is possible only if the button is on screen (the card's «Запустить» always is)."""
    press = option.get("press")
    if press is None:
        return True
    if pending.get("card") and press in ("task:launch", "Запустить"):
        return True
    return any(press in (b.get("text"), b.get("data")) for b in pending.get("buttons", []))


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
                               f"buttons={[b.get('text') for b in pending.get('buttons', [])]}")
    answers.append(pick.answer)
    log.append({"index": index, "slot": pick.slot, "tag": pick.tag, "seed": seed})
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    answers_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending_path.unlink()
    return EXIT_ANSWERED, f"run {run}: {pick.slot} -> {pick.answer}" + (f" [{pick.tag}]" if pick.tag else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.answer_user", description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="the run id (sim/pending_answers/<id>.json)")
    parser.add_argument("--persona", required=True, help="the persona JSON (scripts/sim/personas/<p>.json)")
    parser.add_argument("--seed", type=int, default=0, help="the seed of the random choice (default 0)")
    parser.add_argument("--sim", default=str(ROOT / "sim"), help="the bench folder (default: <repo>/sim)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[\w.-]{1,80}", args.run):
        parser.error("--run: letters, digits, '.', '-', '_' only")
    persona = json.loads(Path(args.persona).read_text(encoding="utf-8"))
    code, message = answer_pending(Path(args.sim).resolve(), args.run, persona, args.seed)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
