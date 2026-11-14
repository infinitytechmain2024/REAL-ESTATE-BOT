"""Q18 (``PORTAL_DESCRIPTION_FULL``): verbatim pieces of the whole listing text around the task's must-have and
exclusion words, for the reviewer.

The reviewer reads 900 characters of the original (``relevance.MAX_REVIEW_TEXT_CHARS``); with the full portal
description a «piscina» at character 2000 would never reach it. ``quote_hits`` returns windows of ±``WINDOW`` characters
around each word of a term, so the reviewer can quote it (``reviewer.haystack_of`` includes them).

A task's terms are often Russian and the listing Spanish: ``SYNONYMS`` maps the frequent features (a Russian stem
of the term -> Spanish and English words). A term not in it is searched as it is. Matching ignores case and accents
and starts at a word start; the windows are slices of the text with its whitespace collapsed (as the excerpt is), so a
quote taken from them is found in the folded haystack.

Q45 (``REVIEW_TENANT_HITS``, off by default; rubric rule «Жилец»): for a person buying a home (``home_purchase``) the
runner adds ``tenant_terms()``, two terms given as their own folded words (a tuple, not a task word): a tenant or an
occupant («alquilada», «ocupada», «sin posesión», «nuda propiedad» ...) and an auction («subasta», «pujas» ...), so the
reviewer sees them even far in the description. Off: the terms are only the task's, as before.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

WINDOW = 120
HITS_PER_TERM = 2
MAX_HITS = 8
MIN_TERM_CHARS = 4

# A Russian stem found in the task's term -> the words searched in the listing (already folded: lower, no accents).
# A Latin word matches as a whole word (an «s» / «es» plural allowed): «lift» never «lifting»; a Cyrillic needle
# (the term itself) matches from a word start («бассейн» in «бассейном»).
# «plaza» alone is too broad («plaza mayor», «a dos minutos de la plaza»): only the parking phrases; «seguridad»
# alone is a door («puerta de seguridad»): only the guard words.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "бассейн": ("piscina", "pool"),
    "лифт": ("ascensor", "lift", "elevator"),
    "террас": ("terraza", "terrace"),
    "гараж": ("garaje", "garage", "parking", "plaza de garaje", "plaza de aparcamiento", "plaza de parking"),
    "парков": ("garaje", "garage", "parking", "aparcamiento", "plaza de garaje", "plaza de aparcamiento",
               "plaza de parking"),
    "сад": ("jardin", "garden"),
    "кондиционер": ("aire acondicionado", "climatizacion", "air conditioning"),
    "мебел": ("amueblado", "amueblada", "furnished"),
    "меблир": ("amueblado", "amueblada", "furnished"),
    "животн": ("mascota", "animales", "pets"),
    "питом": ("mascota", "animales", "pets"),
    "балкон": ("balcon", "balcony"),
    "вид на мор": ("vistas al mar", "vista al mar", "sea view", "sea views"),
    "пляж": ("playa", "beach", "beachfront", "primera linea"),
    "первая лини": ("primera linea", "beachfront", "frontline"),
    "первой лини": ("primera linea", "beachfront", "frontline"),
    "камин": ("chimenea", "fireplace"),
    "кладов": ("trastero", "storage room"),
    "подвал": ("sotano", "bodega", "basement"),
    "отоплен": ("calefaccion", "heating", "suelo radiante"),
    "охран": ("vigilancia", "conserje", "portero", "concierge", "security guard", "seguridad 24"),
    "консьерж": ("conserje", "portero", "concierge"),
    "барбекю": ("barbacoa", "bbq", "barbecue"),
    "соляри": ("solarium",),
}
# Q45: the words of a home sold with a tenant or occupant, and of an auction (folded). The reviewer reads the negations
# («no está ocupada»): a hit only shows where the word occurs. Q46: a match right after a negation («sin inquilinos»,
# «libre de ocupantes», «no alquilada») takes no window of these terms, so it never crowds out a real «alquilada».
TENANT_WORDS: tuple[str, ...] = (
    "alquilada", "alquilado", "arrendada", "arrendado", "arrendatario", "arrendataria", "inquilino", "inquilina",
    "ocupada", "ocupado", "okupada", "okupado", "okupa", "ocupantes", "sin posesion", "nuda propiedad", "usufructo",
    "renta antigua", "contrato de alquiler", "tenant", "tenanted", "sitting tenant", "rented out")
AUCTION_WORDS: tuple[str, ...] = ("subasta", "puja", "cesion de remate", "auction")
HOME_TYPES = ("apartment", "house")
# Q46: a negation right before a word of the Q45 terms (folded, the text's whitespace collapsed).
_NEGATED = re.compile(r"(?<!\w)(?:no|sin|nunca|libres? de)\s$")
_LATIN_END = re.compile(r"[a-z0-9]$")


def fold_chars(text: str) -> str:
    """Lower case without accents, one character for one character (the offsets stay those of ``text``)."""
    return "".join(((unicodedata.normalize("NFKD", c)[:1] or c).lower()[:1] or c) for c in text)


def needles(term: str) -> list[str]:
    """The folded words searched for one task term: the term itself and the synonyms of the stems it contains."""
    folded = " ".join(fold_chars(term).split())
    found = [folded] if len(folded) >= MIN_TERM_CHARS else []
    for stem, words in SYNONYMS.items():
        if re.search(rf"(?<!\w){re.escape(fold_chars(stem))}", folded):  # «й» folds to «и» on both sides
            found += [w for w in words if w not in found]
    return found


def tenant_terms() -> list[tuple[str, ...]]:
    """Q45: the tenant and the auction terms for ``quote_hits`` (each a tuple of folded words); the runner puts them
    before the task's words (Q46), so they get the first windows."""
    return [TENANT_WORDS, AUCTION_WORDS]


def home_purchase(hard: dict, vertical: str | None) -> bool:
    """Q45: the task is to buy a home -- not investors, deal «sale», type apartment or house (``hard_criteria``)."""
    return vertical != "investors" and hard.get("deal") == "sale" and hard.get("type") in HOME_TYPES


def _pattern(needle: str) -> str:
    """From a word start; a Latin needle also to a word end (a plural «s» / «es» allowed)."""
    end = r"(?:e?s)?(?!\w)" if _LATIN_END.search(needle) else ""
    return rf"(?<!\w){re.escape(needle)}{end}"


def _merge(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _needles_of(term: object) -> Iterable[str]:
    """A task word's needles; a tuple (Q45 ``tenant_terms``) is already its folded needles."""
    if isinstance(term, str):
        return needles(term)
    return [w for w in term if isinstance(w, str) and w] if isinstance(term, tuple) else ()


def quote_hits(text: str, terms: Iterable[str | tuple[str, ...]], *, skip_before: int = 0) -> list[str]:
    """Windows of ``text`` (whitespace collapsed) around the words of ``terms``, in text order.

    At most ``HITS_PER_TERM`` windows per term and ``MAX_HITS`` in all, taken round-robin over the terms (the first
    window of every term, then the second ...), so six «jardín» never push out a «piscina». Windows that overlap
    are merged. A match ending before ``skip_before`` (the part the reviewer already reads as the excerpt) is skipped,
    and so is a match of a tuple term (Q45 ``tenant_terms``) right after «no», «sin», «nunca», «libre de» (Q46).
    """
    flat = " ".join(str(text or "").split())
    folded = fold_chars(flat)
    per_term: list[list[tuple[int, int]]] = []
    for term in terms:
        negations = isinstance(term, tuple)  # Q46: only the Q45 terms; «sin piscina» of a must-have stays a hit
        spans = [(max(0, m.start() - WINDOW), min(len(flat), m.end() + WINDOW))
                 for needle in _needles_of(term)
                 for m in re.finditer(_pattern(needle), folded) if m.end() > skip_before
                 and not (negations and _NEGATED.search(folded, max(0, m.start() - 12), m.start()))]
        if spans:
            per_term.append(_merge(spans)[:HITS_PER_TERM])
    chosen: list[tuple[int, int]] = []
    for rank in range(HITS_PER_TERM):
        for windows in per_term:
            if rank < len(windows) and len(chosen) < MAX_HITS:
                chosen.append(windows[rank])
    return [flat[start:end].strip() for start, end in _merge(chosen)]
