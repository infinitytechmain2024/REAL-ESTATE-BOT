"""The Russian finding card: what a user reads in Telegram for one finding.

One pure function, ``render_card``, used by the analysis digest and by the
campaign runner (which streams each finding to the requester). It reads the
stored ``findings.structured_payload`` (any schema version; old payloads
without the analysis-v3 fields still render), the post's text (only to guess
its language) and
an optional task, and returns plain text:

* Russian labels only; a field that is unknown is left out;
* the order of the fields follows the task (a budget puts Цена first, a rent or
  sale request puts Сделка/Тип first, investors see Кто first);
* the main text is the Russian summary; the card names the original language
  («Язык оригинала: испанский») but never quotes the post (its text carries
  Facebook's own buttons such as «Показать оригинал»).

No ids, model names, prompts or English system text reach the card.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

MAX_CARD_CHARS = 3900  # Telegram allows 4096; the runner adds a short footer.
MAX_SUMMARY_CHARS = 1200

DEALS = {"rent": "аренда", "sale": "продажа"}
PROPERTIES = {
    "apartment": "квартира",
    "room": "комната",
    "house": "дом",
    "studio": "студия",
    "land": "участок",
    "commercial": "коммерческая недвижимость",
}
CURRENCIES = {"EUR": "€", "USD": "$", "GBP": "£", "RUB": "₽", "UAH": "₴"}
SOURCES = {
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "instagram.com": "Instagram",
    "t.me": "Telegram",
    "idealista.com": "Idealista",
    "fotocasa.es": "Fotocasa",
    "milanuncios.com": "Milanuncios",
    "yaencontre.com": "Yaencontre",
    "pisos.com": "Pisos.com",
    "habitaclia.com": "Habitaclia",
    "indomio.es": "Indomio",
    "tucasa.com": "Tucasa",
    "kyero.com": "Kyero",
    "thinkspain.com": "ThinkSpain",
    "terrenos.es": "Terrenos.es",
    "solvia.es": "Solvia",
    "alisedainmobiliaria.com": "Aliseda",
    "servihabitat.com": "Servihabitat",
    "sareb.es": "Sareb",
    "altamirainmuebles.com": "Altamira",
    "haya.es": "Haya",
    "hogaria.net": "Hogaria",
    "spainhouses.net": "SpainHouses",
    "green-acres.es": "Green-Acres",
}


@dataclass(frozen=True)
class CardTask:
    """What the requester asked for; decides which fields lead the card."""

    vertical: str = "real_estate"  # real_estate | investors | both
    deal: str | None = None  # rent | sale
    max_price: int | None = None
    rooms: int | None = None


def confidence_words(value: object) -> str | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number >= 0.8:
        return "высокая"
    if number >= 0.5:
        return "средняя"
    return "низкая"


def detect_language(text: str) -> str | None:
    """A last-resort guess when neither the model nor the filter named the language."""
    lower = text.lower()
    if re.search(r"[іїєґ]", lower):
        return "uk"
    if re.search(r"[а-яё]", lower):
        return "ru"
    if re.search(r"[ñ¿¡]|\b(el|la|los|las|piso|alquil\w*|habitaci\w*|venta|euros?)\b", lower):
        return "es"
    if re.search(r"[a-z]", lower):
        return "en"
    return None


def _number(value: float) -> str:
    whole = int(value)
    text = f"{whole:,}".replace(",", " ") if value == whole else f"{value:,.2f}".replace(",", " ")
    return text


def _price(payload: Mapping[str, Any], deal: str | None) -> str | None:
    amount, currency = payload.get("price_amount"), payload.get("price_currency")
    if isinstance(amount, int | float) and not isinstance(amount, bool) and amount > 0:
        unit = CURRENCIES.get(str(currency or "").upper(), str(currency or "").upper())
        text = f"{_number(float(amount))} {unit}".strip()
        return f"{text} в месяц" if deal == "rent" else text
    signals = [str(s).strip() for s in payload.get("price_signals") or [] if str(s).strip()]
    return ", ".join(signals[:3]) or None


def _source(link: str) -> str | None:
    host = (urlparse(link).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if not host:
        return None
    for domain, name in SOURCES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


def also_on(links: Sequence[Mapping[str, Any]]) -> str | None:
    """«Также на: Fotocasa, Milanuncios» plus one bullet per link, for the same object seen elsewhere."""
    urls = [(str(x.get("url") or "").strip(), str(x.get("site") or "").strip()) for x in links if isinstance(x, Mapping)]
    urls = [(u, site) for u, site in urls if u]
    if not urls:
        return None
    sites: list[str] = []
    for url, site in urls:
        name = _source(url) or site
        if name and name not in sites:
            sites.append(name)
    head = "Также на: " + ", ".join(sites) if sites else "Также на:"
    return head + "\n" + "\n".join(f"• {u}" for u, _ in urls)


def _trim(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


LANGUAGE_NAMES = {
    "es": "испанский", "en": "английский", "ru": "русский", "uk": "украинский", "ca": "каталанский",
    "fr": "французский", "de": "немецкий", "it": "итальянский", "pt": "португальский", "pl": "польский",
}


def render_card(
    payload: Mapping[str, Any],
    *,
    original: str = "",
    task: CardTask | None = None,
    vertical: str | None = None,
    language: str | None = None,
    confidence: float | None = None,
    limit: int = MAX_CARD_CHARS,
    cluster_links: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Build the Russian card for one finding; it names the original language, never quotes the post."""
    task = task or CardTask(vertical=vertical or "real_estate")
    kind = vertical or ("investors" if task.vertical == "investors" else "real_estate")
    investors = kind == "investors"

    source_language = str(payload.get("source_language") or "").strip().lower() or None
    lang = source_language or (language if language and language != "unknown" else None)
    original_text = (original or "").strip() or str(payload.get("summary") or "").strip()
    guessed = detect_language(original_text)
    if not source_language and lang == "ru" and guessed == "uk":
        lang = "uk"  # the keyword filter reads every Cyrillic post as ru
    lang = lang or guessed

    deal = payload.get("deal_type") if payload.get("deal_type") in DEALS else None
    fields: dict[str, str | None] = {
        "Кто": _trim(str(payload["who"]), 200) if payload.get("who") else None,
        "Сделка": DEALS.get(deal) if deal else None,
        "Тип": PROPERTIES.get(str(payload.get("property_type") or "")),
        "Цена": _price(payload, deal),
        "Локация": _trim(str(payload["location"]), 200) if payload.get("location") else None,
        "Комнаты": str(payload["rooms"]) if isinstance(payload.get("rooms"), int) and payload["rooms"] > 0 else None,
        # analysis-v6 (absent in older payloads)
        "Район": _trim(str(payload["district"]), 120) if payload.get("district") else None,
        "Этаж": str(payload["floor"]) if isinstance(payload.get("floor"), int) and not isinstance(payload["floor"], bool) else None,
        "Особенности": ", ".join(str(f) for f in payload["features"][:6]) if isinstance(payload.get("features"), list) and payload["features"] else None,
    }
    if investors:
        order = ["Кто", "Локация", "Цена"]
    else:
        order = ["Сделка", "Тип", "Цена", "Локация", "Район", "Комнаты", "Этаж", "Особенности"]
        if task.deal:
            order = ["Сделка", "Тип"] + [f for f in order if f not in ("Сделка", "Тип")]
        if task.max_price:
            order = ["Цена"] + [f for f in order if f != "Цена"]
    labels = {"Цена": "Сумма"} if investors else {}
    lines = [f"{labels.get(name, name)}: {fields[name]}" for name in order if fields.get(name)]
    words = confidence_words(confidence if confidence is not None else payload.get("confidence"))
    if words:
        lines.append(f"Уверенность: {words}")

    summary = str(payload.get("summary_ru") or "").strip()
    if not summary:
        # An analysis-v1/v2 payload has only the summary in the post's language.
        summary = str(payload.get("summary") or "").strip()
    link = str(payload.get("original_post_link") or "").strip()
    links = [str(x).strip() for x in payload.get("related_links") or [] if str(x).strip() and str(x).strip() != link][:3]

    head = ["📈 Инвестиции" if investors else "🏠 Недвижимость", *lines]
    parts = ["\n".join(head)]
    if summary:
        parts.append(f"Кратко: {_trim(summary, MAX_SUMMARY_CHARS)}")
    tail = []
    if link:
        tail.append(f"Ссылка: {link}")
        if source := _source(link):
            tail.append(f"Источник: {source}")
    if links:
        tail.append("Ещё ссылки:\n" + "\n".join(f"• {x}" for x in links))
    if extra := also_on(cluster_links):
        tail.append(extra)
    if tail:
        parts.append("\n".join(tail))

    if lang:
        language_line = f"Язык оригинала: {LANGUAGE_NAMES.get(lang, lang)}"
        if tail:
            parts[-1] += "\n" + language_line
        else:
            parts.append(language_line)
    return "\n\n".join(parts)[:limit]
