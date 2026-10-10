"""Per-campaign totals (PLAN 4.4): the ``campaign_metrics`` row and the text of ``/campaign report``.

The row is recomputed from the tables that already hold the facts -- ``web_search_queries``, ``web_campaign_urls``
(the fetch ``layer`` of each page, migration 039) and ``campaign_findings`` -- in one statement
(``PostgresCampaignStore.refresh_metrics``). The runner refreshes it while a campaign runs (every
``RunnerConfig.metrics_seconds``) and once when it ends; the report command refreshes it again before it prints.

Counting rules: ``findings`` is every ``campaign_findings`` row; ``exact`` the exact cards that are not duplicates;
``similar`` / ``other`` the findings filed in those buckets (held, or sent after the person approved them);
``excluded`` the rejected ones by reason category (``place``, ``deal``, ``budget`` ...); ``duplicates`` the findings
attached to another card. Pages count only real reads (a result kept from its search snippet is no page).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

REFRESH_SQL = """
with q as (
    select count(*)::int as queries, coalesce(sum(result_count), 0)::int as results
      from web_search_queries where campaign_id = $1::uuid and state = 'searched'),
u as (
    select count(*) filter (where state = 'fetched' and layer = 'http' and coalesce(detail, '') <> 'search_snippet')::int as pages_http,
           count(*) filter (where state = 'fetched' and layer = 'render' and coalesce(detail, '') <> 'search_snippet')::int as pages_render,
           count(*) filter (where state = 'fetched' and layer = 'scrape' and coalesce(detail, '') <> 'search_snippet')::int as pages_scrape,
           count(*) filter (where state = 'fetched' and layer = 'api' and coalesce(detail, '') <> 'search_snippet')::int as pages_api,
           count(*) filter (where state = 'failed')::int as pages_failed
      from web_campaign_urls where campaign_id = $1::uuid),
f as (
    select count(*)::int as findings,
           count(*) filter (where bucket = 'exact' and state <> 'duplicate')::int as exact,
           count(*) filter (where bucket = 'similar')::int as "similar",
           count(*) filter (where bucket = 'other')::int as other,
           count(*) filter (where state = 'duplicate')::int as duplicates
      from campaign_findings where campaign_id = $1::uuid),
x as (
    select coalesce(jsonb_object_agg(why, n), '{}'::jsonb) as excluded
      from (select coalesce(why, 'ai') as why, count(*)::int as n from campaign_findings
             where campaign_id = $1::uuid and bucket = 'excluded' and state <> 'duplicate' group by 1) e)
insert into campaign_metrics (campaign_id, queries, results, pages_http, pages_render, pages_scrape, pages_api, pages_failed,
                              findings, exact, "similar", other, excluded, duplicates, updated_at)
select $1::uuid, q.queries, q.results, u.pages_http, u.pages_render, u.pages_scrape, u.pages_api, u.pages_failed,
       f.findings, f.exact, f."similar", f.other, x.excluded, f.duplicates, now()
  from q, u, f, x
on conflict (campaign_id) do update
   set queries = excluded.queries, results = excluded.results, pages_http = excluded.pages_http,
       pages_render = excluded.pages_render, pages_scrape = excluded.pages_scrape, pages_api = excluded.pages_api,
       pages_failed = excluded.pages_failed, findings = excluded.findings, exact = excluded.exact,
       "similar" = excluded."similar", other = excluded.other, excluded = excluded.excluded,
       duplicates = excluded.duplicates, updated_at = now()
returning *"""

SELECT_SQL = "select * from campaign_metrics where campaign_id = $1::uuid"


@dataclass(frozen=True, slots=True)
class CampaignMetrics:
    campaign_id: str
    queries: int = 0
    results: int = 0
    pages_http: int = 0
    pages_render: int = 0
    pages_scrape: int = 0
    pages_failed: int = 0
    findings: int = 0
    exact: int = 0
    similar: int = 0
    other: int = 0
    excluded: dict[str, int] = field(default_factory=dict)  # reason category -> count
    duplicates: int = 0
    updated_at: datetime | None = None
    pages_api: int = 0

    @property
    def pages_read(self) -> int:
        return self.pages_http + self.pages_render + self.pages_scrape + self.pages_api

    @property
    def rejected(self) -> int:
        return sum(self.excluded.values())


def metrics_of(row: Any) -> CampaignMetrics:
    """A ``campaign_metrics`` row (asyncpg record or mapping) as ``CampaignMetrics``."""
    import json

    excluded = row["excluded"]
    if isinstance(excluded, str):
        excluded = json.loads(excluded)
    return CampaignMetrics(
        campaign_id=str(row["campaign_id"]), queries=row["queries"], results=row["results"],
        pages_http=row["pages_http"], pages_render=row["pages_render"], pages_scrape=row["pages_scrape"],
        pages_api=row["pages_api"],
        pages_failed=row["pages_failed"], findings=row["findings"], exact=row["exact"], similar=row["similar"],
        other=row["other"], excluded={str(k): int(v) for k, v in (excluded or {}).items()},
        duplicates=row["duplicates"], updated_at=row["updated_at"])


# Kept in step with ``final_report`` (REASONS, task_title, plural) by tests/test_campaign_metrics.py; copied here
# because the Orchestra dispatcher runs in an image that does not carry the model/analysis packages.
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
_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "RUB": "₽", "UAH": "₴"}
_DEALS = {"sale": "покупка", "rent": "аренда"}
_KINDS = {"apartment": "квартира", "house": "дом", "land": "участок", "commercial": "коммерческая недвижимость"}


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 объявление», «3 объявления», «5 объявлений»."""
    tail = n % 100
    word = many if 11 <= tail <= 14 else one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many
    return f"{n} {word}"


def campaign_title(campaign: Any) -> str:
    """The task in the person's words (the report heading): «квартира · Валенсия · покупка · до 200 000 €»."""
    from .tolerance import request_for

    plan = campaign.plan
    request = request_for(plan.constraints, location=plan.location, vertical=plan.vertical,
                          text=f"{campaign.source_text} {plan.goal}", country=plan.country)
    parts = [_KINDS.get(request.property_type or ""), plan.location_aliases.get("ru") or request.location,
             _DEALS.get(request.deal or "")]
    if request.amount:
        amount = f"{round(request.amount):,}".replace(",", " ") + f" {_SYMBOLS.get(request.currency, request.currency)}"
        parts.append(("до " if request.is_max else "") + amount)
    if request.rooms:
        parts.append(f"от {request.rooms} комн.")
    if request.min_area:
        parts.append(f"от {round(request.min_area)} м²")
    return " · ".join(p for p in parts if p) or plan.goal


def _ads(n: int) -> str:
    return plural(n, "объявление", "объявления", "объявлений")


def report_text(metrics: CampaignMetrics, title: str = "", *, technical: bool = True) -> str:
    """The metrics in Russian. ``technical`` (owners): queries, results and the pages per fetch layer too;
    everyone else gets the findings and the number of pages read, no queries or layers."""
    lines = [f"📊 Отчёт по поиску{f': {title}' if title else ''}"]
    if technical:
        if metrics.queries or metrics.results:
            lines.append(f"Запросов в поиск: {metrics.queries} · результатов: {metrics.results}")
        pages = f"Страниц прочитано: {metrics.pages_read}"
        if metrics.pages_read:
            layers = [f"{name} {n}" for name, n in (("HTTP", metrics.pages_http), ("браузер", metrics.pages_render),
                                                    ("Scrape API", metrics.pages_scrape),
                                                    ("API порталов", metrics.pages_api)) if n]
            pages += f" ({' · '.join(layers)})"
        if metrics.pages_failed:
            pages += f" · не открылось: {metrics.pages_failed}"
        if metrics.pages_read or metrics.pages_failed:
            lines.append(pages)
    elif metrics.pages_read:
        lines.append(f"Страниц прочитано: {metrics.pages_read}")
    if not metrics.findings:
        lines.append("Объявлений пока не найдено.")
    else:
        lines.append(f"Найдено: {_ads(metrics.findings)}")
        lines.append(f"Точных: {metrics.exact} · похожих: {metrics.similar} · других: {metrics.other} · "
                     f"повторов: {metrics.duplicates} · отклонено: {metrics.rejected}")
    if metrics.excluded:
        order = list(REASONS)
        for reason, count in sorted(metrics.excluded.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99,
                                                                             kv[0])):
            lines.append(f"  · {REASONS.get(reason, REASONS['ai'])}: {count}")
    if technical and metrics.updated_at is not None:
        lines.append(f"Данные на {metrics.updated_at.strftime('%H:%M')} UTC")
    return "\n".join(lines)
