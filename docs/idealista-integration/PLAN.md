# Implementation Plan

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
