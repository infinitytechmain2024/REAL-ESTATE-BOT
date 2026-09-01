"""Rendering results as Telegram messages.

Everything user-visible is escaped for HTML parse mode; result text comes from
web pages via an LLM and must never be trusted to be markup-safe.
"""

from __future__ import annotations

from bot.models.enums import Mode
from bot.models.result import StoredResult
from bot.utils.text import TELEGRAM_MESSAGE_LIMIT, escape_html, plural_ru, truncate
from bot.utils.urls import domain_of

_SUMMARY_LIMIT = 700
"""Long enough to be useful, short enough that ten results stay scannable."""


def format_result(result: StoredResult, index: int, total: int) -> str:
    """One result as an HTML message body."""
    facts = result.raw or {}

    lines: list[str] = [
        f"<b>{escape_html(truncate(result.title or domain_of(result.url), 120))}</b>",
        "",
        escape_html(truncate(result.summary, _SUMMARY_LIMIT)),
    ]

    details: list[str] = []
    for label, key in (("📍", "location"), ("💶", "price"), ("📐", "area")):
        value = facts.get(key)
        if value:
            details.append(f"{label} {escape_html(str(value))}")
    if details:
        lines += ["", " · ".join(details)]

    contacts = facts.get("contacts") or []
    if contacts:
        shown = ", ".join(escape_html(str(c)) for c in contacts[:3])
        lines += ["", f"☎️ {shown}"]

    why = facts.get("why_relevant")
    if why:
        lines += ["", f"<i>{escape_html(truncate(str(why), 200))}</i>"]

    lines += [
        "",
        f'🔗 <a href="{escape_html(result.url)}">{escape_html(domain_of(result.url))}</a>',
        f"<i>{index}/{total} · релевантность {result.score}%</i>",
    ]

    body = "\n".join(lines)
    # Results are built from page text, so cap defensively rather than trusting
    # the LLM to have respected the summary limit.
    return body[:TELEGRAM_MESSAGE_LIMIT]


def format_summary(*, mode: Mode, sent: int, hits: int, duplicates: int, degraded: bool) -> str:
    """The closing message after all results have been sent."""
    if sent == 0:
        parts = ["😕 По этому запросу ничего подходящего не нашлось."]
        if duplicates:
            parts.append(f"Пропущено ранее показанных: {duplicates}.")
        parts.append("Попробуйте уточнить локацию, бюджет или тип объекта.")
        return "\n".join(parts)

    # Phrased as separate clauses on purpose: "из N найденных ссылок" needs the
    # adjective to agree with the numeral too, which no simple helper gets right.
    results_noun = plural_ru(sent, "результат", "результата", "результатов")
    parts = [f"✅ Готово: {sent} {results_noun}. Всего просмотрено ссылок: {hits}."]
    if duplicates:
        parts.append(f"Пропущено ранее показанных: {duplicates}.")
    if degraded:
        parts.append(
            "⚠️ ИИ-ранжирование было недоступно — показаны результаты поиска "
            "без фильтрации и с описаниями из поисковой выдачи."
        )
    parts.append(f"Отправьте следующий запрос в режиме «{mode.title}» или смените режим.")
    return "\n".join(parts)
