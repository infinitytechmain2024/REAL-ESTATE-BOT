# Приёмка фаз 4–5

Проверка проведена по [CHECKLIST.md](CHECKLIST.md) на ветке `claude/optimistic-feynman-y4ayed`.
Полный pytest: **1512 passed, 0 skipped** (PostgreSQL и golden включены), Ruff чисто;
`bash -n scripts/apply_migrations.sh` и `git diff --check` чисто.

| Условие | Доказательство и предел |
|---|---|
| Source → common pipeline | `sources/apify.py`, `_from_sources` в `worker.py`; тесты `test_listing_source_worker.py`, `test_web_search_postgres.py`. Факты идут через обычный post/analysis; живой output актора пока не получен. |
| `via/layer=api`, retry failed, HTML breaker | `store.py`, Memory и PostgreSQL тесты `test_web_search.py`, `test_web_search_postgres.py`; fetched/snippet/active claims не перехватываются. |
| `pages_api` и отчёт | Миграция 045; `metrics.py` считает только fetched API, исключая snippet; `SiteReport.read_api` в обоих stores, `summary.site_lines` пишет «Idealista (API)» при реальном API read. Проверяют `test_campaign_metrics.py`, `test_campaign_summary.py`, тесты stores. |
| Расход Apify/Scrape.do | `costs.py` и `worker._scrape`: отдельные стадии `api` и `scrape`; `test_final_report.py` проверяет две суммы и явную строку неподтверждённой оценки, `test_web_layers.py` — замену оценки фактическими Scrape.do credits. |
| Секреты | `APIFY_TOKEN`, `WEB_SEARCH_SCRAPE_API_KEY` имеют `repr=False`; `.env.example` пустые, ключ Scrape.do ограничен HTTPS `api.scrape.do`, HTTPX info-лог редактируется, ошибки не хранят URL/ключ. `test_web_layers.py` проверяет лог и ошибку. |
| Остальные порталы | Полный pytest включает прежние web search, summary, final report и PostgreSQL/golden сценарии. Доступность сайтов на VPS этим не проверяется. |

Живые условия следующего этапа остаются открытыми: реальная земельная схема и площадь участка,
точный location ID Мадрида, полнота выдачи/пагинация, тариф аккаунта и общий жёсткий лимит $1
для всех платных сервисов. Код фаз 4–5 не вызывает платные API по умолчанию; VPS не менялся.
