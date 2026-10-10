# Implementation Plan

## Phase −1 — Быстрые фиксы без новых сервисов (добавлено, см. SCRAPING_ANALYSIS.md §4)
- [x] Allowlist порталов клиента (`WEB_SEARCH_DOMAIN_POLICY=strict`), словари и научные сайты в чёрном списке
- [x] Слово сделки в каждом запросе; фильтр сделки по пути и заголовку на любом сайте
- [x] Отчёт: «площадь не указана» / «ИИ-проверка не сработала» / «бюджет исчерпан» раздельно, ошибки ИИ по кодам; analysis-worker не теряет посты молча
- [x] Парсер площади и сделки (испанские числа, гектары, parcela vs construida), JSON-LD `plot_m2`, `verify_facts` не верит «2» из «m2»
- [x] Circuit breaker по сайту (`WEB_SEARCH_HOST_BREAKER_REFUSALS`), включая scrape API
- [x] Бюджет прогона `CAMPAIGN_BUDGET_USD`, журнал `campaign_costs` (миграция 042), расход по этапам в отчёте
- [x] Префильтр до LLM (сделка, площадь); дедуп URL уже был (`web_seen_urls`, `url_key`) — покрыт тестом
- Миграция Idealista в фазе 1 получит номер **043**

## Step 4 — Проверка провайдеров (2026-10-10)
- [x] Публичные первичные источники: пять акторов Apify, Scrape.do, официальный Idealista API
- [x] `PROVIDERS.md`: цены, схема/ограничения, таблица всех 20 доменов и оценки разработки
- [x] Владелец выбрал `axlymxp/idealista-scraper` + Scrape.do fallback и подтвердил переход к фазе 0 (2026-10-10)

## Phase 0 — Подготовка (Orchestrator + Architecture)
- [x] Orchestrator запускает Architecture Agent
- [x] Architecture Agent подтверждает точки интеграции и протокол ListingSource — `DESIGN.md`

## Phase 1 — Foundation
- [x] Implementation Agent создаёт `bot/web_search/sources/`
- [x] Реализует `ListingSource` + `SourceListing` (plot_m2 отдельно)
- [x] Database Agent готовит миграцию `043_listing_sources.sql`, оба списка apply_migrations.sh, тесты Memory/реального PostgreSQL

Проверки фазы 1: **1421 passed, 0 skipped**, ruff чисто, golden **70 passed**. Ревью принято. Контрольная остановка перед фазой 2.

## Phase 2 — Apify Integration
- [x] Implementation Agent пишет `ApifyIdealistaSource`
- [x] Resilience Agent добавляет обработку ошибок, cap, безопасный abort/billing и синтетические тесты
- [x] Интеграция в `WebSearchWorker._new_round` через `_from_sources`; durable claim/cache/offset (044), default-off конфиг

Проверки фазы 2: **1491 passed, 0 skipped**, Ruff чисто. Схема input проверена публично; живой output и площадь участка не проверены.

## Phase 3 — Store & Worker
- [x] Database Agent + Implementation Agent обновляют enqueue / begin_fetch / finish_fetch в Memory и Postgres
- [x] Поддержка layer=api, повтор failed URL, корректные host counters, сохранение pause/queue policy и разделение статусов api/unlocker

Проверки фазы 3: **1508 passed, 0 skipped**, Ruff чисто; [REVIEW_PHASE_3.md](REVIEW_PHASE_3.md). Контрольная остановка перед фазой 4.

## Phase 4 — Scrape.do
- [ ] Integration Agent настраивает Scrape.do через существующий ScrapeApiClient

## Phase 5 — Reporting & Polish
- [ ] Учёт layer="api" в отчётах и метриках
- [ ] Reviewer Agent проводит финальную проверку по CHECKLIST.md
