"""Rendering results as Telegram messages.

Everything user-visible is escaped for HTML parse mode; result text comes from
web pages via an LLM and must never be trusted to be markup-safe.
"""

from __future__ import annotations

from bot.models.enums import BudgetFit, Mode
from bot.models.query import ParsedQuery
from bot.models.result import StoredResult
from bot.utils.text import TELEGRAM_MESSAGE_LIMIT, escape_html, plural_ru, truncate
from bot.utils.urls import domain_of

_SUMMARY_LIMIT = 700
"""Long enough to be useful, short enough that ten results stay scannable."""


def format_money(amount: float, currency: str | None = None) -> str:
    """A round figure with thin spacing: 45 000 EUR."""
    body = f"{amount:,.0f}".replace(",", " ")
    return f"{body} {currency}".strip() if currency else body


def format_budget_badge(result: StoredResult) -> str | None:
    """The line that says how far outside the budget this result sits."""
    if not result.is_alternative or result.budget_delta is None:
        return None

    amount = format_money(result.budget_delta, result.budget_currency)
    if result.budget_fit is BudgetFit.OVER:
        return f"⬆️ Дороже вашего бюджета на {escape_html(amount)}"
    return f"⬇️ Дешевле вашего бюджета на {escape_html(amount)}"


def format_alternatives_notice(query: ParsedQuery, alternatives: list[StoredResult]) -> str:
    """Shown when nothing matched the budget but near misses were found.

    Names the gap concretely -- "ближайшее дороже на 45 000 EUR" -- because
    that is the number the user needs in order to decide whether to widen the
    budget or the search area.
    """
    segment = _budget_phrase(query)
    lines = [
        f"😕 В этом ценовом сегменте{segment} ничего подходящего не нашлось.",
        "",
    ]

    over = [r for r in alternatives if r.budget_fit is BudgetFit.OVER and r.budget_delta]
    under = [r for r in alternatives if r.budget_fit is BudgetFit.UNDER and r.budget_delta]

    if over:
        nearest = min(r.budget_delta or 0 for r in over)
        currency = over[0].budget_currency
        noun = plural_ru(len(over), "вариант", "варианта", "вариантов")
        lines.append(
            f"⬆️ Есть {len(over)} {noun} дороже — ближайший на "
            f"{escape_html(format_money(nearest, currency))} выше вашего потолка."
        )
    if under:
        nearest = min(r.budget_delta or 0 for r in under)
        currency = under[0].budget_currency
        noun = plural_ru(len(under), "вариант", "варианта", "вариантов")
        lines.append(
            f"⬇️ Есть {len(under)} {noun} дешевле — ближайший на "
            f"{escape_html(format_money(nearest, currency))} ниже вашей нижней границы."
        )

    lines += ["", "Показываю их ниже — по возрастанию разницы с бюджетом."]
    return "\n".join(lines)


def _budget_phrase(query: ParsedQuery) -> str:
    """' (до 300 000 EUR)' or similar, for the no-match notice."""
    currency = query.currency
    if query.budget_min and query.budget_max:
        return (
            f" ({format_money(query.budget_min)}–{format_money(query.budget_max, currency)})"
        )
    if query.budget_max:
        return f" (до {format_money(query.budget_max, currency)})"
    if query.budget_min:
        return f" (от {format_money(query.budget_min, currency)})"
    return ""


def format_result(result: StoredResult, index: int, total: int) -> str:
    """One result as an HTML message body."""
    facts = result.raw or {}

    lines: list[str] = [
        f"<b>{escape_html(truncate(result.title or domain_of(result.url), 120))}</b>",
        "",
        escape_html(truncate(result.summary, _SUMMARY_LIMIT)),
    ]

    badge = format_budget_badge(result)
    if badge:
        lines += ["", f"<b>{badge}</b>"]

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
    else:
        lines += ["", "☎️ Контактная информация: не указана"]

    seller = facts.get("seller")
    if seller:
        lines += ["", f"👤 Продавец: {escape_html(str(seller))}"]

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


def format_summary(
    *,
    mode: Mode,
    sent: int,
    hits: int,
    duplicates: int,
    degraded: bool,
    alternatives: int = 0,
) -> str:
    """The closing message after all results have been sent."""
    if sent == 0:
        parts = ["😕 По этому запросу ничего подходящего не нашлось."]
        if duplicates:
            parts.append(f"Пропущено ранее показанных: {duplicates}.")
        parts.append("Попробуйте уточнить локацию, бюджет или тип объекта.")
        return "\n".join(parts)

    # Phrased as separate clauses on purpose: "из N найденных ссылок" needs the
    # adjective to agree with the numeral too, which no simple helper gets right.
    exact = sent - alternatives
    if alternatives and exact:
        parts = [
            f"✅ Готово: {exact} "
            + plural_ru(exact, "результат", "результата", "результатов")
            + f" в бюджете и ещё {alternatives} рядом с ним."
            + f" Всего просмотрено ссылок: {hits}."
        ]
    elif alternatives:
        parts = [
            f"✅ Готово: {alternatives} "
            + plural_ru(alternatives, "вариант", "варианта", "вариантов")
            + f" вне запрошенного бюджета. Всего просмотрено ссылок: {hits}."
        ]
    else:
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
