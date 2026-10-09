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
- [ ] Владелец выбирает провайдера и подтверждает переход к фазе 0 (контрольная остановка HANDOFF)

## Phase 0 — Подготовка (Orchestrator + Architecture)
- [ ] Orchestrator запускает Architecture Agent
- [ ] Architecture Agent подтверждает точки интеграции и протокол ListingSource

## Phase 1 — Foundation
- [ ] Implementation Agent создаёт `bot/web_search/sources/`
- [ ] Реализует `ListingSource` + `SourceListing`
- [ ] Database Agent готовит миграцию `042_listing_sources.sql`

## Phase 2 — Apify Integration
- [ ] Implementation Agent пишет `ApifyIdealistaSource`
- [ ] Resilience Agent добавляет полную обработку ошибок
- [ ] Интеграция в `WebSearchWorker._new_round` через `_from_sources`

## Phase 3 — Store & Worker
- [ ] Database Agent + Implementation Agent обновляют `begin_fetch` / `finish_fetch`
- [ ] Поддержка `layer="api"` и повторный захват failed URL

## Phase 4 — Scrape.do
- [ ] Integration Agent настраивает Scrape.do через существующий ScrapeApiClient

## Phase 5 — Reporting & Polish
- [ ] Учёт layer="api" в отчётах и метриках
- [ ] Reviewer Agent проводит финальную проверку по CHECKLIST.md
