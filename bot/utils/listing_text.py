"""Cheap, deterministic reading of a listing's area and deal from Spanish (and English/Russian) text.

Used before any model sees a page (``bot.analysis_pipeline.prefilter``; it lives in ``bot/utils`` so the analysis
image has it). Spanish writes thousands with a dot and decimals with a comma: «2.000 m²» is two thousand square
metres, «2,5 ha» two and a half hectares. A number is an area only next to a unit (m², m2, metros cuadrados, ha,
hectáreas, сотки); a bare «500 m» or «200 metros» is a distance. The words just before a number say whose area it is:
the plot (parcela, terreno, solar, finca) or the building (construida, útil, vivienda).

``deal_of`` reads the deal only from strong signals (an offer phrase or a monthly price), and says nothing when a
text shows both deals (a site menu «Venta · Alquiler»).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

AreaKind = Literal["plot", "built"]

_NUMBER = r"\d{1,3}(?:[.,   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_UNIT = (r"m²|m2|mts?2|mts?\.?\s*cuadrados|metros?\s+cuadrados|sq\.?\s*m|sqm"
         r"|hect[aá]reas?|hectares?|ha\b|has\b|га\b|гектар\w*|сот\w*|м²|м2|кв\.?\s*м")
_AREA = re.compile(rf"(?<![\d.,])({_NUMBER})\s*({_UNIT})", re.IGNORECASE)
_PLOT_WORDS = re.compile(r"(?<!\w)(?:parcela|terreno|solar|finca|suelo|plot|land|участ|земл|ділянк)\w*", re.IGNORECASE)
_BUILT_WORDS = re.compile(r"(?<!\w)(?:construid|[uú]til|vivienda|edificad|habitable|built|living|house|home|chalet|casa|floor|дом|жил)\w*",
                          re.IGNORECASE)
WINDOW = 40  # characters before a number searched for whose area it is; the nearest word wins


@dataclass(frozen=True, slots=True)
class Area:
    m2: float
    kind: AreaKind | None = None  # None: the text does not say


def number(raw: str) -> float | None:
    """An area number with Spanish dots or English commas as thousands separators."""
    text = raw.replace(" ", " ").replace(" ", " ").strip()
    if re.fullmatch(r"\d{1,3}(?:[. ]\d{3})+(?:,\d+)?", text):
        whole, _, frac = text.partition(",")
        text = re.sub(r"[. ]", "", whole) + (f".{frac}" if frac else "")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    elif re.fullmatch(r"\d+,\d+", text):
        text = text.replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _to_m2(value: float, unit: str) -> float:
    unit = _fold(unit)
    if unit.startswith(("hect", "гект")) or unit in ("ha", "has", "га"):
        return value * 10_000
    if unit.startswith("сот"):
        return value * 100
    return value


def areas(text: str) -> list[Area]:
    """Every area the text states, in m², with whose area it is when the words before it say so."""
    out: list[Area] = []
    for found in _AREA.finditer(text or ""):
        value = number(found.group(1))
        if value is None:
            continue
        m2 = _to_m2(value, found.group(2))
        if not 1 <= m2 <= 100_000_000:
            continue
        before = re.split(r"[.;·|\n]\s", text[max(0, found.start() - WINDOW):found.start()])[-1]  # this sentence only
        plot = max((m.end() for m in _PLOT_WORDS.finditer(before)), default=-1)
        built = max((m.end() for m in _BUILT_WORDS.finditer(before)), default=-1)
        kind: AreaKind | None = None if plot == built == -1 else "plot" if plot > built else "built"
        # English listings also put the noun immediately after the measure:
        # "10,100m2 plot" or "180m2 built area". An immediate suffix is more
        # specific than a preceding mention of the other area in the sentence.
        after = text[found.end():found.end() + 20].lstrip()
        if _PLOT_WORDS.match(after):
            kind = "plot"
        elif _BUILT_WORDS.match(after):
            kind = "built"
        out.append(Area(round(m2, 1), kind))
    return out


def plot_area(text: str) -> float | None:
    """The largest plot area the text states (parcela/terreno ...), else None."""
    plots = [a.m2 for a in areas(text) if a.kind == "plot"]
    return max(plots) if plots else None


def largest_area(text: str) -> float | None:
    """The largest area of any kind (a plot is never smaller than the house on it), else None."""
    found = [a.m2 for a in areas(text)]
    return max(found) if found else None


_RENT = re.compile(r"(?<!\w)(?:se\s+alquila|en\s+alquiler|alquilo|alquiler(?:es)?|arrendamiento|for\s+rent|to\s+let"
                   r"|сда[её]тся|сдам|аренда|оренда)(?!\w)|€\s*/\s*mes\b|€\s*al\s+mes\b|eur(?:os)?\s*/\s*mes\b"
                   r"|/\s*month\b|per\s+month\b|al\s+mes\b", re.IGNORECASE)
_SALE = re.compile(r"(?<!\w)(?:se\s+vende|en\s+venta|vendo|venta|for\s+sale|продаж\w*|продам|продаю)(?!\w)", re.IGNORECASE)


def deal_of(text: str) -> Literal["rent", "sale"] | None:
    """``rent`` / ``sale`` when the text shows only that deal, else None (neither, or both)."""
    rent, sale = bool(_RENT.search(text or "")), bool(_SALE.search(text or ""))
    return "rent" if rent and not sale else "sale" if sale and not rent else None


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


# The words that make a listing an auction, not an ordinary sale (PORTAL_FACTS_FROM_ROW, Q16): the portal rows
# (``bot.web_search.sources.portals``) and, only with that flag, ``bot.campaign.tolerance.deal_kind_of``
# (CAMPAIGN_AUCTION_DEVIATION; without the flag it keeps its own «subasta(s)», «puja»). Whole words on folded
# text. «adjudicación» is not here: «procedente de adjudicación bancaria» is an ordinary bank sale and «adjudicación de
# obras» is no sale at all.
AUCTION_WORDS = re.compile(r"(?<!\w)(?:subastas?|pujas?|cesion(?:es)?\s+del?\s+remate)(?!\w)")
# A negation right before the word: «no es subasta», «no es una subasta», «sin subasta», «ni subasta»,
# «no procede de subasta».
_AUCTION_NEGATION = re.compile(
    r"(?<!\w)(?:sin|ni|no\s+(?:es|son|hay|se\s+trata\s+de|procede|proceden|viene|vienen)(?:\s+(?:de|del|una?|la|el))*)\s+$")


def auction_named(*texts: object) -> bool:
    """True when the texts name an auction («subasta», «en proceso de subasta», «puja», «cesión de remate») that
    is not negated just before («no es subasta», «sin subasta», «no procede de subasta»)."""
    folded = _fold(" ".join(str(t or "") for t in texts))
    return any(not _AUCTION_NEGATION.search(folded[max(0, hit.start() - 40):hit.start()])
               for hit in AUCTION_WORDS.finditer(folded))


# PORTAL_RENTAL_TERM (Q20). The rental term a task asks for, read from the person's own words only: the task text,
# the interview answers (``TaskSpec.context.answers[*].answer``, never the bot's questions, whose examples name every
# term: «на год и дольше, от 6 месяцев, на 1–3 месяца»), ``must_have``, ``wishes`` and ``notes``. Folded text
# (``_fold``: lower case, no accents; «й» -> «и»). «от 6 месяцев» names no term: neither a year nor a season.
_LONG_TERM = re.compile(
    r"(?<!\w)(?:на\s+(?:1\s+|один\s+|целыи\s+)?(?:год|рік)(?!\w)"
    r"|на\s+(?:\d+\s*[-–]\s*)?(?:\d+|два|три|четыре|пять|несколько|пару)\s+(?:года|лет|роки|років)(?!\w)"
    r"|от\s+(?:1\s+|одного\s+)?года(?!\w)|від\s+(?:1\s+|одного\s+)?року(?!\w)"
    r"|(?:на|от|від)\s+12\s+(?:месяц|місяц)\w*"
    r"|(?:минимум|не\s+менее|хотя\s+бы|мінімум)\s+(?:на\s+)?(?:1\s+|один\s+|одного\s+)?(?:год|года|рік|року)(?!\w)"
    r"|год\s+и\s+(?:дольше|более|больше)|рік\s+і\s+(?:довше|більше)"
    r"|долгосрочн\w*|довгостроков\w*|длительн(?:ыи|ого)\s+срок\w*|на\s+долгии\s+срок"
    r"|long[\s-]?term|larga\s+duracion|largo\s+plazo|por\s+anos|un\s+ano\s+o\s+mas)")
_SHORT_TERM = re.compile(
    r"(?<!\w)(?:на\s+(?:лето|зиму|сезон|літо)(?!\w)"
    r"|на\s+(?:\d+\s*[-–]\s*)?(?:(?:[1-9]|1[01])(?!\d)|один|два|три|четыре|пять|несколько|пару)\s+(?:месяц|місяц)\w*"
    r"|на\s+(?:\d+\s*[-–]\s*)?(?:\d+|один|два|три|четыре|пять|несколько|пару)\s+(?:недел|тижн|дн|сут)\w*"
    r"|на\s+(?:месяц|неделю|місяць|тиждень)(?!\w)"
    r"|посуточн\w*|подобов\w*|краткосрочн\w*|короткостроков\w*|временн\w*\s+аренд\w*|временно(?!\w)"
    r"|short[\s-]?term|temporary|temporada|temporal|vacacional)")
# A negation among the 3 words before a term, within its clause (no «,.;:!?» between): «не обязательно на год»,
# «без долгосрочного контракта», «not long term». «минимум ... не менее» inside a term is the term's own word.
_TERM_NEGATIONS = frozenset({"не", "ни", "нет", "без", "not", "no", "non", "sin", "ні"})
_CLAUSE_END = re.compile(r"[,.;:!?|()\n]")


def _term_named(pattern: re.Pattern[str], folded: str) -> bool:
    """A hit of ``pattern`` without a negation among the 3 words before it in its clause."""
    for hit in pattern.finditer(folded):
        clause = _CLAUSE_END.split(folded[:hit.start()])[-1]
        if not _TERM_NEGATIONS.intersection(re.findall(r"\w+", clause)[-3:]):
            return True
    return False


def task_rental_term(spec: object, *texts: object) -> Literal["long", "short"] | None:
    """«long» when the person's words ask for a long lease («на год», «долгосрочно», «long term») and name no short
    one; «short» for the reverse («на лето», «на 1–3 месяца», «посуточно»); None when neither or both are named.
    A term with a negation among the 3 words before it («не обязательно на год», «not long term») is not named."""
    parts = [str(t or "") for t in texts]
    if isinstance(spec, dict):
        context = spec.get("context")
        answers = context.get("answers") if isinstance(context, dict) else None
        for qa in answers if isinstance(answers, list) else ():
            if isinstance(qa, dict):
                parts.append(str(qa.get("answer") or ""))
        must = spec.get("must_have")
        if isinstance(must, list):
            parts.extend(str(m) for m in must if isinstance(m, str))
        wishes = spec.get("wishes")
        for wish in wishes if isinstance(wishes, list) else ():
            if isinstance(wish, dict):
                parts.append(str(wish.get("text") or ""))
        parts.append(str(spec.get("notes") or ""))
    folded = " ".join(_fold(" | ".join(parts)).split())
    long, short = _term_named(_LONG_TERM, folded), _term_named(_SHORT_TERM, folded)
    return "long" if long and not short else "short" if short and not long else None


# RENT_DEFAULT_LONG_TERM (Q49). A listing that says it is a seasonal, temporary or holiday let, read from its own
# words (folded text, ``_fold``). Sim b4-g5-s5 («снять piso в Барселоне до 1500», term «не знаю пока»): «alquiler de
# temporada», «alquiler temporal (máximo 11 meses)», «uso exclusivo vacaciones/ocio», «estancias vacacionales» went out
# as exact. «larga temporada» / «larga estancia» / «larga duración» are long lets: «temporada» right after «larga» is no
# sign, and a negation among the 4 words before the sign in its clause («no temporal», «no se alquila por temporada»,
# «sin temporada») is none either.
_SEASONAL_LET = re.compile(
    r"(?<!\w)(?:temporadas?|temporal(?:es)?|temporalidad|vacacional(?:es)?"
    r"|(?:uso|para|ocio\s+[oy])\s+(?:exclusivo\s+)?(?:de\s+|para\s+)?vacaciones|vacaciones\s*/\s*ocio"
    r"|por\s+meses|(?:hasta|maximo|max\.?)\s+(?:de\s+)?(?:[1-9]|1[01])\s+meses"
    r"|estancias?\s+cortas?|cortas?\s+estancias?|media\s+estancia"
    r"|vacation\s+rentals?|holiday\s+(?:rentals?|lets?)|seasonal\s+(?:rentals?|lets?)"
    r"|short[\s-]?term\s+(?:rentals?|lets?|stays?)|temporary\s+(?:rentals?|lets?|stays?)"
    r"|(?:временн|сезонн|посуточн|краткосрочн)\w*\s+аренд\w*|посуточн\w*|аренд\w*\s+(?:только\s+)?для\s+отдыха)(?!\w)")
_SEASONAL_NEGATIONS = frozenset({"no", "sin", "ni", "nunca", "not", "non", "never", "не", "без"})


def seasonal_let_named(*texts: object) -> bool:
    """True when the texts say the let is seasonal, temporary or for holidays («alquiler de temporada», «temporal»,
    «vacacional», «por meses», «hasta 11 meses», «estancias cortas», «vacation rental», «временная аренда»); never
    for «larga temporada» or a sign with a negation among the 4 words before it in its clause («no temporal»)."""
    folded = " ".join(_fold(" ".join(str(t or "") for t in texts)).split())
    for hit in _SEASONAL_LET.finditer(folded):
        clause = _CLAUSE_END.split(folded[:hit.start()])[-1]
        words = re.findall(r"\w+", clause)
        if hit.group(0).startswith("temporada") and words and words[-1] in ("larga", "largas"):
            continue
        if not _SEASONAL_NEGATIONS.intersection(words[-4:]):
            return True
    return False
