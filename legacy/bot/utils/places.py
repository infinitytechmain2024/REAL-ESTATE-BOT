"""Whether a piece of text is about the place the user asked for.

Group titles, page titles and snippets arrive in whatever language their author
writes in, while the extracted location is normalised to English -- when the
model manages it, and it does not always. So both sides are folded down to a
bare Latin alphabet before they are compared: "Madrid", "Мадрида" and
"madrileño" become one token, and a group about Bulgaria stays one about
Bulgaria.

The comparison is a shared five-character prefix rather than equality, which is
what carries the declensions ("Мадриду"), the adjectives ("madrileño") and the
small disagreements between transliteration schemes ("Валенсия" -> "valensiya"
against "valencia"). It is a heuristic, and a deliberately cheap one: the cost
of a miss is a group that is not shown, the cost of a false match is a group
that is, and neither is worth a gazetteer.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from bot.models.query import Location

_PREFIX = 5
"""Characters that must agree. Four would match "mala"ga/"mala"sia."""

# ru/uk/bg -> Latin. Only the letters; the scheme does not have to match any
# standard, it has to be applied identically to both sides of a comparison.
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "ё": "e",
    "є": "e", "ж": "zh", "з": "z", "и": "i", "і": "i", "ї": "i", "й": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_QUALIFIERS = (
    # Words that describe a position relative to a place rather than a place
    # itself. They travel in `location.raw` -- "в пригороді Мадриду" -- and
    # would otherwise match any group that happens to use them. Compared by
    # the same prefix rule as everything else, so one entry covers a word's
    # cases: "prigorod" also drops "prigorode" and "prigorodi".
    "suburb", "outskirts", "near", "around", "region", "province", "area",
    "district", "centre", "center", "prigorod", "peredmist", "okrestnosti",
    "okraina", "oblast", "raion",
)

_WORD = re.compile(r"[a-z0-9]+")


def fold(value: str) -> list[str]:
    """*value* as lowercase Latin words, transliterated and stripped of accents."""
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = "".join(_CYRILLIC.get(char, char) for char in value)
    return _WORD.findall(value)


def place_tokens(location: Location) -> list[str]:
    """The words that identify *location*, most reliable form first.

    Prefers the normalised fields. Falls back to the user's own wording, which
    is the only place left when the model did not normalise anything -- the
    case where every location-aware step downstream is already degraded and a
    filter is needed most.
    """
    named = [part for part in (location.city, location.region, location.country) if part]
    source = named or ([location.raw] if location.raw else [])
    tokens: list[str] = []
    for part in source:
        for word in fold(part):
            if len(word) < 4 or word in tokens or _is_qualifier(word):
                continue
            tokens.append(word)
    return tokens


def mentions_place(text: str, tokens: Sequence[str]) -> bool:
    """Whether *text* names any of *tokens*. No tokens means no opinion."""
    if not tokens:
        return True
    words = fold(text)
    return any(_same_place(word, token) for word in words for token in tokens)


def mentions_any(texts: Iterable[str], tokens: Sequence[str]) -> bool:
    return any(mentions_place(text, tokens) for text in texts)


def _is_qualifier(word: str) -> bool:
    return any(_same_place(word, qualifier) for qualifier in _QUALIFIERS)


def _same_place(word: str, token: str) -> bool:
    if min(len(word), len(token)) < _PREFIX:
        return word == token
    return word[:_PREFIX] == token[:_PREFIX]
