# Agents

## 1. Orchestrator Agent
Главный координатор. Не пишет код. Декомпозирует задачи, распределяет их по субагентам, принимает или отклоняет результаты.

**Может использовать:**
- task-decomposition
- architecture-review
- final-acceptance

**Не может:** писать production-код.

---

## 2. Architecture Agent
Отвечает за архитектурную чистоту и минимальную инвазивность изменений.

**Может использовать:**
- codebase-navigation
- protocol-design
- minimal-invasive-change
- architecture-review

**Зона ответственности:**
- Проектирование ListingSource
- Выбор точек интеграции
- Защита пайплайна analysis → tolerance → dedup → reporting

---

## 3. Implementation Agent
Основной разработчик.

**Может использовать:**
- python-async-expert
- pydantic-v2
- apify-client
- web-search-module
- store-layer-extension
- listing-source-protocol

**Зона ответственности:**
- bot/web_search/sources/
- ApifyIdealistaSource
- Изменения worker.py, store.py, models.py, settings.py

---

## 4. Resilience Agent
Отвечает только за устойчивость и обработку ошибок.

**Может использовать:**
- apify-error-handling
- graceful-degradation
- retry-strategies
- logging-observability

**Зона ответственности:**
- TemporaryApifyError / PermanentApifyError
- Поведение при падении Apify
- Fallback-стратегии

---

## 5. Integration Agent
Универсальные unlocker’ы и конфигурация.

**Может использовать:**
- scrape-do
- env-configuration
- layer-3-unlocker

**Зона ответственности:**
- Настройка Scrape.do
- Работа через существующий ScrapeApiClient

---

## 6. Database Agent
Всё, что связано с PostgreSQL и миграциями.

**Может использовать:**
- postgresql-migrations
- asyncpg
- check-constraints
- data-consistency

**Зона ответственности:**
- Миграция 043_listing_sources.sql
- begin_fetch / finish_fetch под layer="api"

---

## 7. Reviewer Agent
Финальный контроль качества.

**Может использовать:**
- code-review
- regression-check
- checklist-validation
- security-light-review

**Зона ответственности:**
- Проверка по CHECKLIST.md
- Отсутствие регрессий
- Базовая безопасность (токены, секреты)
