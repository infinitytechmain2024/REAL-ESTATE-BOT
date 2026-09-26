"""Extra wishes of a real-estate task, read deterministically for «Проверьте задачу».

The summary shows them as normalised Russian phrases ("площадь от 1000 м²",
"до метро 5 мин на машине"), never the person's own words: a voice
transcript must not come back to the user.
"""

from __future__ import annotations

import re

_TYPES = (
    (("участ", "ділянк", "дилянк", "земл", "solar", "terreno", "parcela", "сотк", "соток"), "участок"),
    (("квартир", "piso", "apartamento", "flat"), "квартира"),
    (("комнат", "кімнат", "habitaci"), "комната"),
    (("дом", "будин", "будинок", "casa", "chalet", "вилл", "віл"), "дом"),
    (("офис", "офіс", "локал", "local", "склад", "nave", "коммерч", "комерц"), "коммерческая"),
)
_NUMBER_WORDS = {"тысяч": 1000, "тисяч": 1000}
_AREA = re.compile(
    r"(?:(?P<num>\d[\d\s.,]*)\s*(?P<k>тыс\w*|тис\w*|k|к)?|(?P<word>тысяч\w*|тисяч\w*))\s*"
    r"(?P<unit>м²|м2|m²|m2|кв\.?\s*м\w*|квадрат\w*|метр\w*|метрів|соток|сот\w*|гектар\w*|га\b)",
    re.IGNORECASE,
)
_MINUTES = re.compile(r"(\d{1,3})\s*(?:мин\w*|хв\w*|min\w*)", re.IGNORECASE)


def _area(text: str) -> str | None:
    match = _AREA.search(text)
    if not match:
        return None
    if match.group("word"):
        value = 1000
    else:
        raw = re.sub(r"[\s.,]", "", match.group("num"))
        if not raw.isdigit():
            return None
        value = int(raw) * (1000 if match.group("k") else 1)
    unit = match.group("unit").casefold()
    if unit.startswith("сот"):
        value, unit_text = value * 100, "м²"
    elif unit.startswith(("гектар", "га")):
        value, unit_text = value * 10_000, "м²"
    else:
        unit_text = "м²"
    if not 0 < value <= 10_000_000:
        return None
    before = text[max(0, match.start() - 12):match.start()].casefold()
    prefix = "от " if any(w in before for w in ("от", "від", "не менее", "больше", "більше", "мінімум", "минимум")) else ""
    return f"площадь {prefix}{value:,} {unit_text}".replace(",", " ")


def property_type(text: str) -> str | None:
    t = text.casefold()
    words = re.findall(r"\w+", t)
    for stems, label in _TYPES:
        if any(w.startswith(stems) for w in words):
            return label
    return None


def details(text: str) -> list[str]:
    """Normalised Russian wishes found in a real-estate task, in a fixed order."""
    t = text.casefold()
    found: list[str] = []
    if (area := _area(text)) is not None:
        found.append(area)
    house_any = re.search(r"(с домом|з будинком)\s*(или|або|чи)\s*без", t)
    if house_any:
        found.append("с домом или без")
    elif re.search(r"\b(с домом|з будинком|con casa)\b", t):
        found.append("с домом")
    if "метро" in t or "metro" in t:
        minutes = _MINUTES.search(t)
        by_car = any(w in t for w in ("машин", "авто", "coche", "car"))
        if minutes:
            found.append(f"до метро {minutes.group(1)} мин" + (" на машине" if by_car else ""))
        else:
            found.append("рядом с метро")
    if any(w in t for w in ("застройк", "забудов", "строительств", "будівництв", "построить", "побудувати", "edificar")):
        found.append("под застройку")
    if any(w in t for w in ("пригород", "передмі", "окрестност", "околиц", "afueras", "alrededores")):
        found.append("пригород")
    return found
