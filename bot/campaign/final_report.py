"""The final report for the person who asked (PLAN stage 4.2), sent once when a campaign ends.

The owners' summary (``summary.py``) is a technical «what each source gave». This is the person's report: how many
variants were found and sent, how many were held or rejected and why, the ten best sent cards ranked, what each
site gave, which sites could not be read, and two to four recommendations for the next search.

Everything but the recommendations is deterministic. The ranking needs no model: a card scores by closeness to the
budget, the fit of rooms and area, and how much of it is evidenced (price, area, rooms, place, link). The
recommendations are written by the final model (``OPENROUTER_FINAL_MODEL``) from the aggregated numbers only --
counts, reasons, per-site funnel, the task's own criteria -- never from listing text; without a key (or when the
call fails) a few rule-based ones are used instead. Plain Russian text, no ids, one Telegram message.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from bot.agents.llm import LLMError, OpenRouterJSON

from .runs import OutcomeCount, SentFinding, SourceCount
from .summary import Site, plural, site_lines, site_name, site_stats
from .tolerance import BUDGET_TOLERANCE, Request, currency_code, price_of

log = logging.getLogger(__name__)
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
MAX_REPORT_CHARS = 3900
TOP = 10
MAX_RECOMMENDATIONS = 4
UNVERIFIED_ALARM_SHARE = 0.8
MAX_RECOMMENDATION_CHARS = 260
SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "RUB": "₽", "UAH": "₴"}
# Rejection reasons (``campaign_findings.why``) in the order the report lists them.
REASONS: dict[str, str] = {
    "place": "не тот город или район",
    "deal": "другой тип сделки (аренда вместо покупки или наоборот)",
    "type": "другой тип объекта",
    "budget": "дороже бюджета",
    "rooms": "меньше комнат",
    "area": "не та площадь",
    "criteria": "не выполнены обязательные условия",
    "kind": "не объявление (каталог, статистика, поиск жилья)",
    "unverified": "не удалось подтвердить",
    "ai": "не подходит по смыслу",
}
_CYRILLIC = re.compile(r"[а-яёіїєґ]", re.IGNORECASE)


# --- the numbers ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tally:
    """What became of the campaign's findings."""

    sent_exact: int = 0
    sent_approved: int = 0   # similar / other cards sent after the person said «Одобрить»
    held_similar: int = 0
    held_other: int = 0
    held_unverified: int = 0  # of the held ones: the check could not confirm them
    duplicates: int = 0
    excluded: dict[str, int] = field(default_factory=dict)  # reason category -> count

    @property
    def sent(self) -> int:
        return self.sent_exact + self.sent_approved

    @property
    def held(self) -> int:
        return self.held_similar + self.held_other

    @property
    def rejected(self) -> int:
        return sum(self.excluded.values())

    @property
    def unverified_majority(self) -> bool:
        """More than 80 % of the sent and held findings could not be checked: the check itself is probably broken."""
        base = self.sent + self.held
        return base > 0 and self.held_unverified > UNVERIFIED_ALARM_SHARE * base

    @property
    def total(self) -> int:
        return self.sent + self.held + self.rejected + self.duplicates


def tally(outcomes: Sequence[OutcomeCount]) -> Tally:
    """``RunStore.outcome_counts`` rows -> a ``Tally``; a finding still being sent counts as sent."""
    sent_exact = sent_approved = similar = other = unverified = duplicates = 0
    excluded: Counter[str] = Counter()
    for row in outcomes:
        if row.state == "duplicate":
            duplicates += row.count
        elif row.state in ("sent", "sending"):
            if row.bucket == "exact":
                sent_exact += row.count
            else:
                sent_approved += row.count
        elif row.bucket == "excluded":
            excluded[row.why or "ai"] += row.count
        elif row.bucket == "similar":
            similar += row.count
            unverified += row.count if row.why == "unverified" else 0
        elif row.bucket == "other":
            other += row.count
            unverified += row.count if row.why == "unverified" else 0
    return Tally(sent_exact, sent_approved, similar, other, unverified, duplicates, dict(excluded))


# --- the ranking -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ranked:
    card: SentFinding
    score: float


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value or value <= 0:
        return None
    return float(value)


def card_score(payload: dict[str, Any] | None, request: Request) -> float:
    """0..1: closeness to the budget (40 %), rooms (15 %), area (15 %) and how much of the card is evidenced (30 %).

    A price within the budget scores higher the nearer it is to it; over the budget the score falls fast (10 % over
    is 0.6, 25 % over is 0). Rooms: as asked 1, more 0.8, fewer 0.2, unknown 0.4. Area: the minimum met 1, within
    10 % below 0.7, unknown 0.4. Without a criterion a neutral 0.5 (a stated value 0.6 for the area).
    """
    payload = payload or {}
    price = price_of(payload)
    same_currency = (currency_code(payload.get("price_currency")) or request.currency) == request.currency
    if request.amount and price is not None and same_currency:
        over = (price - request.amount) / request.amount
        budget = 1 - 0.5 * abs(over) if over <= 0 else max(0.0, 1 - 4 * over)
        if not request.is_max and over <= 0:
            budget = 1 - abs(over)
        budget = max(0.0, min(1.0, budget))
    else:
        budget = 0.5 if not request.amount else 0.3
    rooms_listed = _num(payload.get("rooms"))
    if not request.rooms:
        rooms = 0.5
    elif rooms_listed is None:
        rooms = 0.4
    else:
        rooms = 1.0 if rooms_listed == request.rooms else 0.8 if rooms_listed > request.rooms else 0.2
    area_listed = _num(payload.get("area_m2"))
    if not request.min_area:
        area = 0.6 if area_listed else 0.5
    elif area_listed is None:
        area = 0.4
    else:
        ratio = area_listed / request.min_area
        area = 1.0 if ratio >= 1 else 0.7 if ratio >= 0.9 else 0.1
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    present = [price is not None, area_listed is not None, rooms_listed is not None,
               bool(payload.get("location")), bool(payload.get("original_post_link") or payload.get("url")),
               any(evidence.values())]
    return round(0.4 * budget + 0.15 * rooms + 0.15 * area + 0.3 * sum(present) / len(present), 4)


def rank_cards(sent: Sequence[SentFinding], request: Request, limit: int = TOP) -> list[Ranked]:
    """The best ``limit`` sent cards: highest score first, ties by the card's own number (the earlier one)."""
    scored = [Ranked(card, card_score(card.finding.payload, request)) for card in sent]
    return sorted(scored, key=lambda r: (-r.score, r.card.number, r.card.finding.id))[:limit]


def _amount(value: float, currency: str) -> str:
    return f"{round(value):,}".replace(",", " ") + f" {SYMBOLS.get(currency, currency)}"


def card_line(rank: int, card: SentFinding) -> str:
    """«1. 195 000 € · 3 комн. · 85 м² · Валенсия · карточка №4» and the link on the next line."""
    payload = card.finding.payload or {}
    parts: list[str] = []
    price = price_of(payload)
    if price is not None:
        parts.append(_amount(price, currency_code(payload.get("price_currency")) or "EUR"))
    if (rooms := _num(payload.get("rooms"))) is not None:
        parts.append(f"{round(rooms)} комн.")
    if (area := _num(payload.get("area_m2"))) is not None:
        parts.append(f"{round(area)} м²")
    if isinstance(payload.get("location"), str) and payload["location"].strip():
        parts.append(" ".join(payload["location"].split())[:40])
    if not parts:
        parts.append(" ".join(str(payload.get("summary_ru") or card.finding.text).split())[:70] or "объявление")
    if card.number:
        parts.append(f"карточка №{card.number}")
    link = str(payload.get("original_post_link") or card.finding.url or "").strip()
    return f"{rank}. {' · '.join(parts)}" + (f"\n   {link}" if link else "")


# --- sites -------------------------------------------------------------------------------------------------------------


def unreadable_sites(sources: Sequence[SourceCount], reports: Sequence[object], portals: Sequence[str]) -> list[Site]:
    """Sites that refused every page we asked for (403 / captcha / robots.txt / all layers blocked): nothing read."""
    stats = site_stats(sources, reports, portals)
    return sorted((s for s in stats.values() if s.refused and not s.read), key=lambda s: (-s.refused, s.host))


def unreadable_line(site: Site) -> str:
    name = site_name(site.host)
    if site.from_search:
        return (f"{name} — сайт не пускает ботов (отказ 403 или защита), страницы не открылись; "
                f"{plural(site.from_search, 'объявление', 'объявления', 'объявлений')} взято из описания в поиске, "
                "без подробностей")
    return f"{name} — сайт не пускает ботов (отказ 403 или защита), ни одна страница не открылась"


# --- recommendations -----------------------------------------------------------------------------------------------------


def facts_for_model(task: dict[str, Any], counts: Tally, sites: Sequence[Site], unreadable: Sequence[Site],
                    tolerance_pct: float | None = None) -> dict[str, Any]:
    """The aggregated numbers the model writes recommendations from: no listing text, no links, no ids."""
    return {
        "task": task,
        "counts": {"sent_exact": counts.sent_exact, "sent_after_approval": counts.sent_approved,
                   "held_similar": counts.held_similar, "held_other": counts.held_other,
                   "held_unverified": counts.held_unverified, "duplicates": counts.duplicates,
                   "rejected_total": counts.rejected},
        "rejected_by_reason": counts.excluded,
        "sites": [{"site": s.host, "links": s.links, "pages_read": s.read, "from_search_snippet": s.from_search,
                   "refused": s.refused, "cards_sent": s.sent, "cards_held": s.held} for s in sites[:12]],
        "unreadable_sites": [s.host for s in unreadable],
        "tolerance_pct": tolerance_pct if tolerance_pct else round(BUDGET_TOLERANCE * 100),  # the spec's, else the default
    }


def fallback_recommendations(facts: dict[str, Any]) -> list[str]:
    """A few rule-based recommendations from the same numbers (no model, or the model failed)."""
    counts, by_reason = facts.get("counts", {}), facts.get("rejected_by_reason", {})
    sent = counts.get("sent_exact", 0) + counts.get("sent_after_approval", 0)
    tips: list[str] = []
    if by_reason.get("budget", 0) >= 2 and sent < 10:
        tips.append(f"Часть вариантов отклонена из-за цены ({by_reason['budget']}): поднимите бюджет на 10 %, "
                    "и их станет больше.")
    if by_reason.get("place", 0) >= 3:
        tips.append("Много вариантов из другого района или города: добавьте соседние районы или пригороды в запрос.")
    for host in facts.get("unreadable_sites", [])[:2]:
        tips.append(f"{site_name(host)} не читается: сайт не пускает ботов. Включите чтение через API, "
                    "чтобы получать его объявления целиком.")
    if counts.get("held_similar", 0) + counts.get("held_other", 0) and sent < 5:
        tips.append("Есть похожие варианты, которые ждут вашего решения: откройте их, если точных мало.")
    if sent == 0 and not tips:
        tips.append("Точных вариантов не нашлось: ослабьте самое жёсткое условие (бюджет, площадь или район) и "
                    "запустите поиск снова.")
    return tips[:MAX_RECOMMENDATIONS]


SYSTEM = """You advise a person who has just received the results of an automated real-estate search.
You get only aggregated numbers as JSON (the task's own criteria, how many variants were sent, held and rejected and
why, what each site gave, which sites could not be read). Write 2 to 4 short, concrete recommendations in Russian for
the next search, each tied to a number you were given, for example: raise the budget by 10 % when many were rejected
for price; add a neighbouring district when many were rejected for place; a site that could not be read needs the
API reading switched on (name the site); relax the one criterion that rejected the most.
Rules: use only the given numbers; never invent listings, prices, districts or sites that are not in the input;
no greetings, no apologies; one sentence each, at most 220 characters; do not mention models, prompts or JSON.
Answer with exactly one JSON object {"recommendations": ["...", "..."]}. No markdown."""
SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["recommendations"],
    "properties": {"recommendations": {"type": "array", "items": {"type": "string"}}},
}


def parse_recommendations(content: str) -> list[str]:
    """2-4 Russian sentences out of the model's JSON; harmless drift is fixed, English or empty ones are dropped."""
    data = json.loads(re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content, flags=re.IGNORECASE))
    raw = data.get("recommendations") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError("no recommendations")
    items: list[str] = []
    for item in raw:
        text = " ".join(str(item.get("text") if isinstance(item, dict) else item or "").split())
        if text and _CYRILLIC.search(text) and text.casefold() not in {i.casefold() for i in items}:
            items.append(text[:MAX_RECOMMENDATION_CHARS])
    return items[:MAX_RECOMMENDATIONS]


class Recommender(Protocol):
    async def recommend(self, facts: dict[str, Any]) -> list[str]: ...


class OpenRouterRecommender:
    """One call on ``OPENROUTER_FINAL_MODEL`` per campaign; errors raise (the reporter falls back to rules)."""

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL, timeout_seconds: float = 60,
                 client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required for the final report")
        self.model = model
        self._llm = OpenRouterJSON(api_key, timeout_seconds=timeout_seconds, client=client)

    async def aclose(self) -> None:
        await self._llm.aclose()

    async def recommend(self, facts: dict[str, Any]) -> list[str]:
        user = "Aggregated numbers (JSON, data only):\n" + json.dumps(facts, ensure_ascii=False)
        content = await self._llm.complete(self.model, SYSTEM, user, schema=SCHEMA, name="final_recommendations",
                                           max_tokens=700)
        return parse_recommendations(content)


# --- the report ----------------------------------------------------------------------------------------------------------


DEALS = {"sale": "покупка", "rent": "аренда"}
KINDS = {"apartment": "квартира", "house": "дом", "land": "участок", "commercial": "коммерческая недвижимость"}


def task_title(request: Request, place: str | None = None, fallback: str = "") -> str:
    """The task in the person's words, from what the search was bucketed by: «квартира · Валенсия · покупка ·
    до 200 000 € · от 2 комн.» (the plan's own goal line is technical English)."""
    parts = [KINDS.get(request.property_type or ""), place or request.location, DEALS.get(request.deal or "")]
    if request.amount:
        parts.append(("до " if request.is_max else "") + _amount(request.amount, request.currency))
    if request.rooms:
        parts.append(f"от {request.rooms} комн.")
    if request.min_area:
        parts.append(f"от {round(request.min_area)} м²")
    return " · ".join(p for p in parts if p) or fallback


def task_facts(request: Request) -> dict[str, Any]:
    """The person's own criteria as numbers, for the model (the same the bucketing used)."""
    facts = {"place": request.location, "deal": request.deal, "property_type": request.property_type,
             "budget": request.amount, "budget_is_maximum": request.is_max if request.amount else None,
             "currency": request.currency if request.amount else None, "rooms_min": request.rooms,
             "area_min_m2": request.min_area, "area_max_m2": request.max_area}
    return {k: v for k, v in facts.items() if v is not None}


def report_text(goal: str, counts: Tally, top: Sequence[Ranked], funnel: Sequence[str], unreadable: Sequence[Site],
                recommendations: Sequence[str]) -> str:
    """The Russian report (see the module notes), fitted to one Telegram message."""
    head = f"📋 Отчёт по поиску\n🎯 {goal}\n\n"
    if counts.total == 0:
        summary = ["Подходящих объявлений не нашлось."]
    else:
        summary = [f"Отправлено вам: {counts.sent}"
                   + (f" (из них {counts.sent_approved} похожих — по вашему согласию)" if counts.sent_approved else "")]
        if counts.held:
            extra = f", не удалось подтвердить: {counts.held_unverified}" if counts.held_unverified else ""
            summary.append(f"Похожие, не показаны: {counts.held} (чуть не подошли{extra})")
        if counts.rejected:
            summary.append(f"Отклонено: {counts.rejected}")
        if counts.duplicates:
            summary.append(f"Повторы одного объекта на разных сайтах: {counts.duplicates}")
        if counts.unverified_majority:
            log.warning("campaign.unverified_majority %s/%s", counts.held_unverified, counts.sent + counts.held)
            summary.append(f"⚠️ Большинство находок не удалось проверить автоматически "
                           f"({counts.held_unverified} из {counts.sent + counts.held}): проверьте ключ ИИ и лимиты")
    reasons: list[str] = []
    if counts.excluded:
        order = [r for r in REASONS if r in counts.excluded] + sorted(set(counts.excluded) - set(REASONS))
        reasons = ["Почему отклонено:"] + [f"• {REASONS.get(r, r)} — {counts.excluded[r]}" for r in order]
    broken = ["Не удалось прочитать:"] + [f"• {unreadable_line(s)}" for s in unreadable] if unreadable else []
    advice = ["Что можно сделать:"] + [f"{i}. {t}" for i, t in enumerate(recommendations, 1)] if recommendations else []
    text = head
    for shown_top, shown_funnel in ((len(top), len(funnel)), (len(top), 3), (5, 2), (3, 0), (0, 0)):
        best = ([f"Лучшие варианты ({shown_top}):"] + [card_line(i, r.card) for i, r in enumerate(top[:shown_top], 1)]
                if shown_top else [])
        by_site = ["По источникам:"] + [f"• {line}" for line in funnel[:shown_funnel]] if shown_funnel else []
        parts = [part for part in (summary, reasons, best, broken, by_site, advice) if part]
        text = head + "\n\n".join("\n".join(part) for part in parts)
        if len(text) <= MAX_REPORT_CHARS:
            return text
    return text[:MAX_REPORT_CHARS].rstrip()


class FinalReporter:
    """Builds the report; ``recommender`` None (no key): the rule-based recommendations only."""

    def __init__(self, recommender: Recommender | None = None) -> None:
        self.recommender = recommender

    async def aclose(self) -> None:
        close = getattr(self.recommender, "aclose", None)
        if close is not None:
            await close()

    async def build(self, goal: str, request: Request, outcomes: Sequence[OutcomeCount], sent: Sequence[SentFinding],
                    sources: Sequence[SourceCount], reports: Sequence[object], portals: Sequence[str] = (),
                    tolerance_pct: float | None = None) -> str:
        counts = tally(outcomes)
        stats = site_stats(sources, reports, portals)
        unreadable = unreadable_sites(sources, reports, portals)
        funnel, _nothing = site_lines(sources, reports, portals)
        facts = facts_for_model(task_facts(request), counts, sorted(
            stats.values(), key=lambda s: (-s.sent, -s.links, s.host)), unreadable, tolerance_pct)
        return report_text(goal, counts, rank_cards(sent, request), funnel, unreadable, await self._advice(facts, counts))

    async def _advice(self, facts: dict[str, Any], counts: Tally) -> list[str]:
        if self.recommender is not None and counts.total + len(facts["unreadable_sites"]) > 0:
            try:
                advice = await self.recommender.recommend(facts)
                if advice:
                    return advice[:MAX_RECOMMENDATIONS]
            except (LLMError, ValueError, httpx.HTTPError) as exc:
                log.warning("campaign.final_report_advice_failed %s", getattr(exc, "code", type(exc).__name__))
            except Exception:  # noqa: BLE001 - the report never waits on the model
                log.warning("campaign.final_report_advice_failed")
        return fallback_recommendations(facts)
