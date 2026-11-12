"""Q18 (``PORTAL_DESCRIPTION_FULL``): verbatim pieces of the whole listing text around the task's must-have and
exclusion words, for the reviewer.

The reviewer reads 900 characters of the original (``relevance.MAX_REVIEW_TEXT_CHARS``); with the full portal
description a «piscina» at character 2000 would never reach it. ``quote_hits`` returns windows of ±``WINDOW`` characters
around each word of a term, so the reviewer can quote it (``reviewer.haystack_of`` includes them).

A task's terms are often Russian and the listing Spanish: ``SYNONYMS`` maps the frequent features (a Russian stem
of the term -> Spanish and English words). A term not in it is searched as it is. Matching ignores case and accents
and starts at a word start; the windows are slices of the text with its whitespace collapsed (as the excerpt is), so a
quote taken from them is found in the folded haystack.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

WINDOW = 120
MAX_HITS = 6
MIN_TERM_CHARS = 4

# A Russian stem found in the task's term -> the words searched in the listing (already folded: lower, no accents).
# «plaza» alone is too broad («plaza mayor», «a dos minutos de la plaza»): only the parking phrases.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "бассейн": ("piscina", "pool"),
    "лифт": ("ascensor", "lift", "elevator"),
    "террас": ("terraza", "terrace"),
    "гараж": ("garaje", "garage", "parking", "plaza de garaje", "plaza de aparcamiento", "plaza de parking"),
    "парков": ("garaje", "garage", "parking", "aparcamiento", "plaza de garaje", "plaza de aparcamiento",
               "plaza de parking"),
    "сад": ("jardin", "garden"),
    "кондиционер": ("aire acondicionado", "climatizacion", "air conditioning"),
    "мебел": ("amueblad", "furnished"),
    "меблир": ("amueblad", "furnished"),
    "животн": ("mascota", "animales", "pets"),
    "питом": ("mascota", "animales", "pets"),
}


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


def quote_hits(text: str, terms: Iterable[str], *, skip_before: int = 0) -> list[str]:
    """Windows of ``text`` (whitespace collapsed) around the words of ``terms``, merged when they overlap, at most
    ``MAX_HITS``, in text order. A match ending before ``skip_before`` (the part the reviewer already reads as the
    excerpt) is skipped."""
    flat = " ".join(str(text or "").split())
    folded = fold_chars(flat)
    spans: list[tuple[int, int]] = []
    for term in terms:
        for needle in needles(term) if isinstance(term, str) else ():
            for match in re.finditer(rf"(?<!\w){re.escape(needle)}", folded):
                if match.end() > skip_before:
                    spans.append((max(0, match.start() - WINDOW), min(len(flat), match.end() + WINDOW)))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [flat[start:end].strip() for start, end in merged[:MAX_HITS]]
