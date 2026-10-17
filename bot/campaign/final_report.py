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
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from bot.agents.llm import LLMError, OpenRouterJSON
from bot.utils.costs import CostSummary
from bot.web_search.urls import PORTALS, SPAIN_BANK_PORTALS, SPAIN_PORTALS_BY_KIND, UKRAINE_PORTALS

from .relevance import MANDATORY_UNKNOWN
from .runs import OutcomeCount, SentFinding, SourceCount, StreamFinding
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
    "area_unknown": "площадь не указана",
    "ai_failed": "ИИ-проверка не сработала",
    "cost_cap": "бюджет прогона исчерпан",
    "ai": "не подходит по смыслу",
}
# Held findings that are not «a little off the request» but «not checked»: the data had no area, the reviewer could
# not confirm a criterion, the AI check failed or the run's budget was spent.
UNCHECKED = ("unverified", "area_unknown", "ai_failed", "cost_cap")
# A skip reason of the cost ledger (``bot.utils.costs``) -> what the report calls it.
SKIPS = {"prefilter_deal": "другой тип сделки", "prefilter_area": "участок меньше нужного", "budget_cap": "бюджет исчерпан"}
STAGE_NAMES = {"llm": "ИИ", "fetch": "страницы", "scrape": "Scrape API", "api": "API порталов", "search": "поиск"}
_CYRILLIC = re.compile(r"[а-яёіїєґ]", re.IGNORECASE)
# A recommendation never asks the person to change what they asked for: the deal, the kind of property, the city or country.
_FORBIDDEN_ADVICE = re.compile(
    r"сделк|аренд|покупк|продаж|тип\w*\s+(?:объект|недвижим|сделк)|вид\w*\s+(?:объект|недвижим)"
    r"|друг\w+\s+(?:город|стран)|смен\w+\s+(?:город|стран)|в\s+друг\w+\s+(?:город|стран)", re.IGNORECASE)
_RELAX_ADVICE = re.compile(
    r"(?:сниз|уменьш|ослаб|допуст|подним|повыс)\w*.{0,50}(?:бюджет|площад|комнат|радиус)"
    r"|(?:бюджет|площад|комнат|радиус).{0,50}(?:сниз|уменьш|ослаб|допуст|подним|повыс)"
    r"|соседн\w*.{0,40}(?:район|пригород)|расшир\w*.{0,40}(?:район|географ)", re.IGNORECASE)
_DIGIT = re.compile(r"\d")
MIN_RECOMMENDATIONS = 2
DEAL_NOTE_MIN = 10  # rejected for the deal type: from this many the report explains where they came from
# Sites that are real-estate sources whatever they gave: the portals the search targets (``bot.web_search.urls``).
KNOWN_PORTALS = frozenset(PORTALS) | frozenset(SPAIN_BANK_PORTALS) | frozenset(UKRAINE_PORTALS) | frozenset(
    host for hosts in SPAIN_PORTALS_BY_KIND.values() for host in hosts)


# --- the numbers ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tally:
    """What became of the campaign's findings."""

    sent_exact: int = 0
    sent_approved: int = 0   # similar / other cards sent after the person said «Одобрить»
    held_similar: int = 0
    held_other: int = 0
    held_unverified: int = 0  # of the held ones: the check could not confirm them (every ``UNCHECKED`` reason)
    duplicates: int = 0
    excluded: dict[str, int] = field(default_factory=dict)  # reason category -> count
    unchecked: dict[str, int] = field(default_factory=dict)  # of ``held_unverified``: ``UNCHECKED`` reason -> count
    held_must: int = 0  # of the held ones: a must-have criterion is not mentioned, never offered (H11)

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
    def ai_failed(self) -> int:
        """Findings the AI check never judged: it failed, or the run's budget was spent."""
        return self.unchecked.get("ai_failed", 0) + self.unchecked.get("cost_cap", 0)

    @property
    def unverified_majority(self) -> bool:
        """More than 80 % of the sent and held findings were never judged by the AI check: it is broken (a missing
        area or a criterion the reviewer could not confirm is a fact about the listings, not about the check)."""
        base = self.sent + self.held
        return base > 0 and self.ai_failed > UNVERIFIED_ALARM_SHARE * base

    @property
    def total(self) -> int:
        return self.sent + self.held + self.rejected + self.duplicates


def tally(outcomes: Sequence[OutcomeCount]) -> Tally:
    """``RunStore.outcome_counts`` rows -> a ``Tally``; a finding still being sent counts as sent."""
    sent_exact = sent_approved = similar = other = unverified = duplicates = must = 0
    excluded: Counter[str] = Counter()
    unchecked: Counter[str] = Counter()
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
        elif row.bucket in ("similar", "other"):
            similar += row.count if row.bucket == "similar" else 0
            other += row.count if row.bucket == "other" else 0
            if row.why == MANDATORY_UNKNOWN:
                must += row.count
            if row.why in UNCHECKED:
                unverified += row.count
                unchecked[row.why] += row.count
    return Tally(sent_exact, sent_approved, similar, other, unverified, duplicates, dict(excluded), dict(unchecked), must)


# --- the ranking -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ranked:
    card: SentFinding
    score: float


@dataclass(frozen=True, slots=True)
class HeldEvidence:
    """A held finding and the criterion matrix already saved by the reviewer."""

    finding: StreamFinding
    review: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class PlaceScope:
    """The geographic boundary the person actually approved in the interview."""

    city: str | None = None
    suburb: bool = False
    radius_km: float | None = None
    districts: tuple[str, ...] = ()

    @property
    def unresolved(self) -> bool:
        return self.suburb and not self.radius_km and not self.districts


_SUBURB = re.compile(r"пригород|передміст|окрестност|околиц|suburb|surround|outskirts|afueras|alrededores", re.IGNORECASE)


def place_scope_of(spec: Mapping[str, Any] | None, task_text: str, city: str | None) -> PlaceScope:
    """Use the saved interview scope; a city name alone does not resolve a suburb request."""
    place = spec.get("place") if isinstance(spec, Mapping) else None
    place = place if isinstance(place, Mapping) else {}
    raw_radius = place.get("radius_km")
    radius = (float(raw_radius) if isinstance(raw_radius, (int, float)) and not isinstance(raw_radius, bool)
              and math.isfinite(raw_radius) and 0 < raw_radius <= 1000 else None)
    raw_districts = place.get("districts")
    districts = (tuple(" ".join(x.split())[:80] for x in raw_districts[:8] if isinstance(x, str) and x.strip())
                 if isinstance(raw_districts, list) else ())
    return PlaceScope(city, bool(_SUBURB.search(task_text)), radius, districts)


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
    if site.unverified:  # human verification: the site asked for a person's check and nobody passed it
        return f"{name} — проверку никто не прошёл"
    if site.from_search:
        return (f"{name} — сайт не пускает ботов (отказ 403 или защита), страницы не открылись; "
                f"{plural(site.from_search, 'объявление', 'объявления', 'объявлений')} взято из описания в поиске, "
                "без подробностей")
    return f"{name} — сайт не пускает ботов (отказ 403 или защита), ни одна страница не открылась"


def _known(host: str, portals: Sequence[str] = ()) -> bool:
    return any(host == p or host.endswith("." + p) for p in (*KNOWN_PORTALS, *portals))


def real_estate_sources(sources: Sequence[SourceCount], reports: Sequence[object], portals: Sequence[str] = ()
                        ) -> tuple[list[SourceCount], list[object], int]:
    """The user's view of the sites: only known portals and hosts that gave at least one listing post or finding.

    Returns the sources and reports of those sites and how many other sites the search touched (dictionaries,
    calculators, wikis ...). The owners' summary keeps the raw list.
    """
    stats = site_stats(sources, reports, portals)
    keep = {host for host, s in stats.items() if _known(host, portals) or s.posts or s.sent or s.held or s.unverified}

    def key(host: str) -> str:
        return next((p for p in portals if host == p or host.endswith("." + p)), host)

    kept_sources = [s for s in sources if s.platform != "website" or key(s.name) in keep]
    kept_reports = [r for r in reports if key(str(getattr(r, "host", ""))) in keep]
    return kept_sources, kept_reports, len(stats) - len(keep)


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
                   "refused": s.refused, "unverified": s.unverified, "cards_sent": s.sent, "cards_held": s.held} for s in sites[:12]],
        "unreadable_sites": [s.host for s in unreadable],
        "tolerance_pct": tolerance_pct if tolerance_pct else round(BUDGET_TOLERANCE * 100),  # the spec's, else the default
    }


def fallback_recommendations(facts: dict[str, Any]) -> list[str]:
    """A few rule-based recommendations from the same numbers (no model, or the model failed).

    Only the allowed levers: budget, area, rooms, neighbouring districts, more sources / API, timing. Each cites its
    number. Rejections for the deal type, the kind of property or the country never become advice (the person cannot
    change those); a large deal-type count becomes one explanation instead.
    """
    counts, by_reason = facts.get("counts", {}), facts.get("rejected_by_reason", {})
    only_exact = bool((facts.get("task") or {}).get("only_exact"))
    sent = counts.get("sent_exact", 0) + counts.get("sent_after_approval", 0)
    tips: list[str] = []
    if not only_exact and by_reason.get("budget", 0) >= 2 and sent < 10:
        tips.append(f"Часть вариантов отклонена из-за цены ({by_reason['budget']}): поднимите бюджет на 10 %, "
                    "и их станет больше.")
    if not only_exact and by_reason.get("place", 0) >= 3:
        tips.append(f"{by_reason['place']} вариантов оказались вне нужного района: добавьте соседние районы или "
                    "пригороды в запрос.")
    if not only_exact and by_reason.get("rooms", 0) >= 2 and sent < 10:
        tips.append(f"{by_reason['rooms']} вариантов отклонены из-за числа комнат: допустите на одну комнату меньше.")
    if not only_exact and by_reason.get("area", 0) >= 2 and sent < 10:
        tips.append(f"{by_reason['area']} вариантов отклонены из-за площади: уменьшите минимальную площадь на 10 %.")
    unreadable = facts.get("unreadable_sites", [])
    refused = {s.get("site"): s.get("refused", 0) for s in facts.get("sites", []) if isinstance(s, dict)}
    unverified = {s.get("site") for s in facts.get("sites", []) if isinstance(s, dict) and s.get("unverified")}
    for host in [h for h in unreadable if h not in unverified][:2]:
        tips.append(f"{site_name(host)} не читается (отказов: {refused.get(host) or 1}): сайт не пускает ботов. "
                    "Включите чтение через API или прокси, чтобы получать его объявления целиком.")
    held = counts.get("held_similar", 0) + counts.get("held_other", 0)
    if held and sent < 5 and not only_exact:
        tips.append(f"Есть похожие варианты ({held}), которые ждут вашего решения: откройте их, если точных мало.")
    if held and sent < 5 and only_exact:
        tips.append(f"Похожих вариантов: {held}, но они не подтвердили все условия; уточните недостающие данные у продавцов.")
    if sent == 0 and not tips:
        tips.append("Точных вариантов: 0 — повторите поиск с дополнительными источниками без изменения требований."
                    if only_exact else "Точных вариантов: 0 — ослабьте самое жёсткое условие "
                    "(бюджет, площадь или район) и запустите поиск снова.")
    if sent < 3 and len(tips) < MIN_RECOMMENDATIONS:
        tips.append("Новые объявления появляются каждый день: повторите поиск через 2-3 дня.")
    deal, wrong = (facts.get("task") or {}).get("deal"), by_reason.get("deal", 0)
    if wrong >= DEAL_NOTE_MIN and deal in ("sale", "rent"):
        other = "аренды" if deal == "sale" else "продажи"
        tips = tips[:MAX_RECOMMENDATIONS - 1]
        tips.append(f"{wrong} объявлений {other} попали в выдачу сайтов — они отсеяны автоматически и не мешают "
                    "результату.")
    return tips[:MAX_RECOMMENDATIONS]


def clean_recommendations(items: Sequence[str], facts: dict[str, Any]) -> list[str]:
    """The model's recommendations without those that ask to change the deal, the kind of property or the city, and
    without those that cite no number; topped up from the rules to at least two when fewer are left."""
    only_exact = bool((facts.get("task") or {}).get("only_exact"))
    kept = [t for t in items if not _FORBIDDEN_ADVICE.search(t) and _DIGIT.search(t)
            and not (only_exact and _RELAX_ADVICE.search(t))][:MAX_RECOMMENDATIONS]
    if len(kept) < MIN_RECOMMENDATIONS:
        have = {t.casefold() for t in kept}
        for tip in fallback_recommendations(facts):
            if tip.casefold() not in have and len(kept) < MIN_RECOMMENDATIONS:
                kept.append(tip)
    return kept


SYSTEM = """You advise a person who has just received the results of an automated real-estate search.
You get only aggregated numbers as JSON (the task's own criteria, how many variants were sent, held and rejected and
why, what each site gave, which sites could not be read). Write 2 to 4 short, concrete recommendations in Russian for
the next search, each citing a number you were given (write the number in the sentence), for example: raise the budget
by 10 % when many were rejected for price; relax the area or the rooms when many were rejected for them; add a
neighbouring district when many were rejected for place; a site that could not be read needs the API or a proxy
switched on (name the site); search again in a few days when little was found.
Allowed levers only: budget, area, rooms, neighbouring districts, more sources, proxy or the Idealista API, timing.
When task.only_exact is true, NEVER suggest relaxing the budget, area, rooms, location or any other requirement,
or opening similar variants; suggest checking missing evidence, more sources or repeating the exact search later.
NEVER advise changing the deal type (buy / rent), the kind of property, or the city or country: the person cannot
change those, so do not mention rejections for them in the advice at all; never use the words deal, rent, purchase,
property type, another city.
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


def task_title(request: Request, place: str | None = None, fallback: str = "",
               scope: PlaceScope | None = None) -> str:
    """The task in the person's words, from what the search was bucketed by: «квартира · Валенсия · покупка ·
    до 200 000 € · от 2 комн.» (the plan's own goal line is technical English)."""
    location = place or request.location
    if scope and location:
        if scope.radius_km:
            location = f"окрестности {location}, радиус {scope.radius_km:g} км"
        elif scope.suburb and scope.districts:
            location = f"окрестности {location}: {', '.join(scope.districts[:3])}"
        elif scope.suburb:
            location = f"пригород {location} (границы не уточнены)"
    parts = [KINDS.get(request.property_type or ""), location, DEALS.get(request.deal or "")]
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
             "area_min_m2": request.min_area, "area_max_m2": request.max_area,
             "only_exact": (True if request.budget_pct == 0 and request.area_pct == 0
                            and request.rooms_delta in (None, 0) and not request.nearby else None)}
    return {k: v for k, v in facts.items() if v is not None}


_CRITERION_LABELS = {"place": "местоположение", "deal": "тип сделки", "type": "тип объекта",
                     "budget": "цена", "rooms": "число комнат", "area": "площадь"}
_LEGAL = re.compile(r"застрой|строительств|разрешен|разрешён|зонир|urbaniz|suelo|permit", re.IGNORECASE)


def _criterion_label(name: str) -> str:
    if name.startswith("must_have:") or name.startswith("exclude:"):
        return name.split(":", 1)[1][:90]
    return _CRITERION_LABELS.get(name, name.replace("_", " ")[:90])


def _among(count: int, total: int) -> str:
    if count == total == 1:
        return "У единственного варианта"
    return f"У одного из {total}" if count == 1 else f"У {count} из {total}"


def held_evidence_lines(held: Sequence[HeldEvidence], *, total: int, task_text: str = "",
                        requested_place: str | None = None, min_area: float | None = None,
                        scope: PlaceScope | None = None) -> list[str]:
    """Explain held results using reviewer evidence, without exposing cards before approval.

    A legacy matrix may call marketing language proof of legal buildability. Treat that old pass as unknown.
    Counts describe only the inspected sample when the campaign has more held findings than we loaded.
    """
    if not held:
        return []
    inspected = len(held)
    matrices: list[dict[str, str]] = []
    for item in held:
        review = item.review if isinstance(item.review, dict) else {}
        entries = review.get("criteria") if isinstance(review.get("criteria"), list) else []
        states: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name, verdict = entry["name"], entry.get("verdict")
            if verdict not in ("pass", "fail", "unknown"):
                continue
            if verdict == "pass" and name.startswith("must_have:") and _LEGAL.search(name) and review.get("prompt") != "reviewer-v3":
                verdict = "unknown"
            states[name] = verdict
        matrices.append(states)
    names = list(dict.fromkeys(name for states in matrices for name in states))
    prefix = "Среди " + plural(inspected, "предварительного варианта", "предварительных вариантов",
                              "предварительных вариантов")
    lines = [prefix + ":"]
    if not names:
        lines.append("• Нет сохранённой проверки условий по каждому объекту — степень соответствия неизвестна.")
        return lines
    by_name = {name: Counter(states.get(name, "unknown") for states in matrices) for name in names}
    # Start with decisive gaps in the user's own must-haves, then other missing facts.
    gaps = sorted(((name, counts["unknown"]) for name, counts in by_name.items() if counts["unknown"]),
                  key=lambda pair: (not pair[0].startswith("must_have:"), -pair[1], pair[0]))
    for name, count in gaps[:3]:
        lines.append(f"• {_among(count, inspected)} не подтверждено: {_criterion_label(name)}.")
    failures = sorted(((name, counts["fail"]) for name, counts in by_name.items() if counts["fail"]),
                      key=lambda pair: (-pair[1], pair[0]))
    for name, count in failures[:2]:
        lines.append(f"• {_among(count, inspected)} явно не выполнено: {_criterion_label(name)}.")
    passes = sorted(((name, counts["pass"]) for name, counts in by_name.items() if counts["pass"]),
                    key=lambda pair: (-pair[1], pair[0]))
    for name, count in passes:
        if name in ("place", "deal", "type"):
            continue  # the report should prioritise discriminating facts, not broad category matches
        label = f"площадь от {min_area:g} м²" if name == "area" and min_area else _criterion_label(name)
        lines.append(f"• {_among(count, inspected)} по объявлению проходит условие: {label}.")
        break
    scope = scope or PlaceScope(requested_place, bool(_SUBURB.search(task_text)))
    if scope.suburb or scope.radius_km:
        if scope.unresolved:
            lines.append("• Границы пригородного поиска не уточнены: нужен радиус или названия населённых пунктов.")
        city_addresses = sum(str((item.finding.payload or {}).get("municipality") or "").casefold() ==
                             str(scope.city or "").casefold() for item in held if scope.city)
        if city_addresses:
            lines.append(f"• {_among(city_addresses, inspected)} явно указан муниципалитет {scope.city}; "
                         "соответствие заданному географическому охвату не подтверждено.")
        else:
            suffix = f", {scope.city.strip()}, {scope.city.strip()}".casefold() if scope.city else ""
            city_mentions = sum(suffix in str((item.finding.payload or {}).get("address") or "").casefold()
                                for item in held) if suffix else 0
            if city_mentions:
                uncertainty = ("пригородность по такому адресу не доказана" if scope.suburb else
                               "попадание в заданный радиус по такому адресу не доказано")
                lines.append(f"• {_among(city_mentions, inspected)} адрес содержит {scope.city}; "
                             f"{uncertainty}.")
            else:
                lines.append("• Муниципалитет и попадание в заданный географический охват нужно проверить по точному адресу.")
    if inspected < total:
        remaining = total - inspected
        lines.append(f"Ещё {plural(remaining, 'вариант', 'варианта', 'вариантов')} "
                     + ("в этом разборе не проверен." if remaining == 1 else "в этом разборе не проверены."))
    return lines


def evidence_actions(held: Sequence[HeldEvidence]) -> list[str]:
    """Actions tied to unanswered hard criteria, rather than generic advice to broaden a search."""
    if not held:
        return []
    counts: Counter[str] = Counter()
    for item in held:
        review = item.review if isinstance(item.review, dict) else {}
        entries = review.get("criteria") if isinstance(review.get("criteria"), list) else []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name = entry["name"]
            unknown = entry.get("verdict") == "unknown" or (
                entry.get("verdict") == "pass" and name.startswith("must_have:")
                and _LEGAL.search(name) and review.get("prompt") != "reviewer-v3")
            if unknown:
                counts[name] += 1
    actions: list[str] = []
    for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
        quantity = plural(count, "варианта", "вариантов", "вариантов")
        if _LEGAL.search(name):
            action = f"Запросите документы о разрешённой застройке для {quantity}."
        elif re.search(r"метро|станц|транспорт|пешком|машин", name, re.IGNORECASE):
            action = f"Уточните адрес и проверьте маршрут по условию «{_criterion_label(name)}» для {quantity}."
        elif name == "place":
            action = f"Уточните точный адрес и муниципалитет для {quantity}."
        elif name == "area":
            action = f"Уточните площадь объекта для {quantity}."
        else:
            action = f"Уточните у продавца условие «{_criterion_label(name)}» для {quantity}."
        if action not in actions:
            actions.append(action)
        if len(actions) == 2:
            break
    return actions


def _usd(value: float) -> str:
    return f"${value:.2f}" if value >= 0.01 or value == 0 else "<$0.01"


def cost_lines(spent: CostSummary | None, budget: float = 0.0) -> list[str]:
    """«💶 Расход: $3.20 из $5.00 (ИИ $2.90 · страницы $0.30)», what was dropped before the model, and the model's
    errors by code: the money and the failures of the run, never hidden behind «не удалось подтвердить»."""
    if spent is None:
        return []
    lines: list[str] = []
    stages = " · ".join(f"{STAGE_NAMES.get(k, k)} {_usd(v)}" for k, v in sorted(spent.by_stage.items(), key=lambda kv: -kv[1])
                        if v > 0)
    limit = f" из {_usd(budget)}" if budget > 0 else ""
    lines.append(f"💶 Расход: {_usd(spent.total)}{limit}" + (f" ({stages})" if stages else ""))
    if spent.estimates:
        estimate = sum(spent.estimates.values())
        lines.append(f"Из них оценка без подтверждения провайдера: {_usd(estimate)}")
    if budget > 0 and spent.total >= budget:
        lines.append("⛔ Бюджет прогона исчерпан: поиск и проверки остановлены, часть находок не проверена")
    skips: Counter[str] = Counter()
    for key, n in spent.skips.items():
        skips[SKIPS.get(key.split(":", 1)[-1], key.split(":", 1)[-1])] += n
    if skips:
        lines.append(f"Отсеяно до ИИ (без вызовов ИИ): {sum(skips.values())} — "
                     + ", ".join(f"{name} {n}" for name, n in skips.most_common()))
    errors = Counter({key.split(":", 1)[-1]: n for key, n in spent.errors.items() if key.startswith("llm:")})
    if errors:
        lines.append("⚠️ Ошибки ИИ: " + ", ".join(f"{code} ×{n}" for code, n in errors.most_common(4)))
    api_errors = Counter({key.split(":", 1)[-1]: n for key, n in spent.errors.items() if key.startswith("api:")})
    if api_errors:
        lines.append("⚠️ Ошибки API порталов: " + ", ".join(f"{code} ×{n}" for code, n in api_errors.most_common(4)))
    return lines


@dataclass(frozen=True, slots=True)
class LeadsSummary:
    """The investor branch of a campaign. ``None``: not known (no post read, so no count) -- never a guessed zero."""

    leads_saved: int | None = None
    people_sent: int = 0     # person cards sent (from comments)
    reach_sent: int = 0      # reach contacts sent
    posts_read: int | None = None
    comments_read: int | None = None
    read_lines: tuple[str, ...] = ()  # one line per post read, a failed one with its reason and cost

    @property
    def empty(self) -> bool:
        return not (self.leads_saved or self.people_sent or self.reach_sent or self.posts_read or self.comments_read
                    or self.read_lines)


# Cost-ledger items (``Entry.item``) that identify a branch; every other item stays «общее» (models of the lead and
# reach judges are booked under the model name, the same as the listing judges, so they cannot be told apart).
_INVESTOR_ITEMS = ("facebook-comments", "facebook-posts")
_REAL_ESTATE_ITEMS = ("idealista", "fotocasa", "yaencontre", "portal")
_REAL_ESTATE_STAGES = ("search", "fetch", "scrape")


def cost_split(spent: CostSummary) -> tuple[float, float, float]:
    """``(real estate, investors, common)`` USD of the ledger: the web stages by stage, the Apify actors by ``item``
    (an actor id has no dot, a scraped host has); whatever is not identified is common."""
    real_estate = sum(v for k, v in spent.by_stage.items() if k in _REAL_ESTATE_STAGES)
    investors = 0.0
    for item, value in spent.by_item.items():
        name = item.lower()
        if any(mark in name for mark in _INVESTOR_ITEMS):
            investors += value
        elif "." not in name and any(mark in name for mark in _REAL_ESTATE_ITEMS):
            real_estate += value
    return real_estate, investors, max(spent.total - real_estate - investors, 0.0)


def branch_cost_line(spent: CostSummary | None) -> list[str]:
    if spent is None or spent.total <= 0:
        return []
    real_estate, investors, common = cost_split(spent)
    return [f"По веткам: недвижимость {_usd(real_estate)} · инвесторы {_usd(investors)} · общее {_usd(common)}"]


def investors_section(leads: LeadsSummary | None) -> list[str]:
    """«💼 Инвесторы»: what the investor branch gave."""
    lines = ["💼 Инвесторы"]
    if leads is None or leads.empty:
        return [*lines, "Людей и контактов пока не найдено."]
    if leads.posts_read is not None:
        lines.append(f"Прочитано постов: {leads.posts_read}, комментариев: {leads.comments_read or 0}")
    lines.extend(leads.read_lines)
    if leads.leads_saved is not None:
        lines.append(f"Лидов сохранено: {leads.leads_saved}")
    lines.append(f"Отправлено вам: людей {leads.people_sent}, доставок из поиска {leads.reach_sent}")
    return lines


def report_text(goal: str, counts: Tally, top: Sequence[Ranked], funnel: Sequence[str], unreadable: Sequence[Site],
                recommendations: Sequence[str], other_sites: int = 0, similar_note: str | None = None,
                costs: Sequence[str] = (), evidence: Sequence[str] = (), task_text: str = "", section: str = "",
                reserve: int = 0, must_note: str | None = None) -> str:
    """The Russian report (see the module notes), fitted to one Telegram message. ``costs``: ``cost_lines``.
    ``section``: a heading above the counts (the ``both`` report); ``reserve``: characters left for what follows."""
    limit = MAX_REPORT_CHARS - reserve
    head = f"📋 Отчёт по поиску\n🎯 {goal}\n\n"
    if task_text and task_text.casefold() not in goal.casefold():
        head = f"📋 Отчёт по поиску\n🎯 {goal}\nВаш запрос: {' '.join(task_text.split())[:220]}\n\n"
    if counts.total == 0:
        summary = ["Объявлений для оценки не получено. Подтверждённых совпадений нет."]
    else:
        if counts.sent_exact:
            conclusion = f"Подтверждённых совпадений: {counts.sent_exact}."
        elif counts.held:
            conclusion = "Подтверждённых совпадений нет. Ни один предварительный вариант не прошёл все условия."
        else:
            conclusion = "Подтверждённых совпадений нет."
        summary = [conclusion, f"Отправлено вам: {counts.sent}"
                   + (f" (из них {counts.sent_approved} похожих — по вашему согласию)" if counts.sent_approved else "")]
        if counts.held:
            parts = [f"{REASONS[k]}: {counts.unchecked[k]}" for k in UNCHECKED if counts.unchecked.get(k)]
            extra = f", {', '.join(parts)}" if parts else ""
            summary.append(f"Похожие, не показаны: {counts.held} (условия требуют проверки{extra})"
                           if counts.held_unverified else
                           f"Похожие, не показаны: {counts.held} (соответствие всем условиям не подтверждено)")
            if similar_note and counts.sent == 0:
                summary.append(similar_note)
            if must_note:
                summary.append(must_note)
        if counts.rejected:
            summary.append(f"Отклонено: {counts.rejected}")
        if counts.duplicates:
            summary.append(f"Повторы одного объекта на разных сайтах: {counts.duplicates}")
        if counts.unverified_majority:
            log.warning("campaign.unverified_majority %s/%s", counts.ai_failed, counts.sent + counts.held)
            summary.append(f"⚠️ ИИ-проверка не сработала для большинства находок "
                           f"({counts.ai_failed} из {counts.sent + counts.held}): см. ошибки ИИ ниже")
    if section:
        summary.insert(0, section)
    summary += list(costs)
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
        if by_site and other_sites:
            by_site.append(f"Прочие сайты из поиска: {other_sites} (не относятся к недвижимости или без объявлений)")
        parts = [part for part in (summary, list(evidence), reasons, best, broken, by_site, advice) if part]
        text = head + "\n\n".join("\n".join(part) for part in parts)
        if len(text) <= limit:
            return text
    return text[:limit].rstrip()


def mandatory_note(counts: Tally, held: Sequence[HeldEvidence]) -> str | None:
    """«Ещё N без упоминания: терраса»: the held findings whose must-have is not mentioned (H11)."""
    if not counts.held_must:
        return None
    names: dict[str, None] = {}
    for item in held:
        review = item.review if isinstance(item.review, dict) else {}
        for entry in review.get("criteria") or []:
            if (isinstance(entry, dict) and entry.get("verdict") == "unknown"
                    and str(entry.get("name")).startswith("must_have:")):
                names[_criterion_label(str(entry["name"]))] = None
    return f"Ещё {counts.held_must} без упоминания" + (": " + ", ".join(names) if names else "")


def similar_note(counts: Tally, offers: Mapping[str, str | None] | None) -> str | None:
    """One line under «Отправлено вам: 0» + «Похожие, не показаны»: where the held variants can be had."""
    if offers is None or counts.sent or not counts.held:
        return None
    states = [offers.get("similar") if counts.held_similar else None, offers.get("other") if counts.held_other else None]
    if "asked" in states:
        return "Похожие можно открыть кнопкой «Одобрить» выше"
    if any(st == "declined" for st in states):
        return None
    return "Отправлю похожие, если подтвердите"


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
                    tolerance_pct: float | None = None, offers: Mapping[str, str | None] | None = None,
                    costs: CostSummary | None = None, budget: float = 0.0, task_text: str = "",
                    held: Sequence[HeldEvidence] = (), scope: PlaceScope | None = None,
                    leads: LeadsSummary | None = None, vertical: str = "real_estate") -> str:
        """``offers``: the state of each bucket's question (``offer_state``: None, asked, approved, declined); with it
        a report that sent nothing but holds similar variants says how to get them. ``costs``: the run's ledger
        (``bot.utils.costs``) and ``budget`` its limit: the money, what was dropped before the model, its errors.
        ``vertical``: ``real_estate`` (the text above, unchanged), ``both`` (it as «🏠 Недвижимость», then «💼 Инвесторы»
        from ``leads``, then the money by branch), ``investors`` (the investor section only)."""
        if vertical == "investors":
            head = f"📋 Отчёт по поиску\n🎯 {goal}\n\n"
            return (head + "\n".join(investors_section(leads)) + "\n\n"
                    + "\n".join(cost_lines(costs, budget) + branch_cost_line(costs))).rstrip()[:MAX_REPORT_CHARS]
        counts = tally(outcomes)
        sources, reports, other_sites = real_estate_sources(sources, reports, portals)
        stats = site_stats(sources, reports, portals)
        unreadable = unreadable_sites(sources, reports, portals)
        funnel, _nothing = site_lines(sources, reports, portals)
        facts = facts_for_model(task_facts(request), counts, sorted(
            stats.values(), key=lambda s: (-s.sent, -s.links, s.host)), unreadable, tolerance_pct)
        note = None if facts["task"].get("only_exact") else similar_note(counts, offers)
        evidence = held_evidence_lines(held, total=counts.held, task_text=task_text,
                                       requested_place=request.location, min_area=request.min_area, scope=scope)
        advice = evidence_actions(held)
        advice.extend(tip for tip in await self._advice(facts, counts) if tip not in advice)
        if vertical != "both":
            return report_text(goal, counts, rank_cards(sent, request), funnel, unreadable, advice[:MAX_RECOMMENDATIONS],
                               other_sites, note, cost_lines(costs, budget), evidence, task_text,
                               must_note=mandatory_note(counts, held))
        tail = "\n\n" + "\n".join(investors_section(leads))
        money = cost_lines(costs, budget) + branch_cost_line(costs)
        if money:
            tail += "\n\n" + "\n".join(money)
        return report_text(goal, counts, rank_cards(sent, request), funnel, unreadable, advice[:MAX_RECOMMENDATIONS],
                           other_sites, note, (), evidence, task_text, "🏠 Недвижимость", len(tail),
                           mandatory_note(counts, held)) + tail

    async def _advice(self, facts: dict[str, Any], counts: Tally) -> list[str]:
        if facts["task"].get("only_exact"):
            return fallback_recommendations(facts)
        if self.recommender is not None and counts.total + len(facts["unreadable_sites"]) > 0:
            try:
                advice = clean_recommendations(await self.recommender.recommend(facts), facts)
                if advice:
                    return advice
            except (LLMError, ValueError, httpx.HTTPError) as exc:
                log.warning("campaign.final_report_advice_failed %s", getattr(exc, "code", type(exc).__name__))
            except Exception:  # noqa: BLE001 - the report never waits on the model
                log.warning("campaign.final_report_advice_failed")
        return fallback_recommendations(facts)
