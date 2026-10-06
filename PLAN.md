# PLAN.md — доработка REAL-ESTATE-BOT до качественной выдачи

Основание: `docs/REVIEW_AND_PLAN.md` (ревью кода, найденные дефекты, лимиты).
Этот файл — рабочий чек-лист. Каждая задача: что сделать, где, как проверить.
Статусы: `[ ]` не начато · `[~]` в работе · `[x]` готово · `[!]` заблокировано.

Правила выполнения:
- Одна задача = одна ветка/коммит с тестом. Ничего не пушится без `make lint test`.
- Порядок этапов обязателен: 0 → 1 → 2 → (3 ∥ 4) → 5 → 6.
- После каждого этапа — прогон золотого теста «Валенсия, квартира до 200k, 2+ комнаты»
  и запись результата в `docs/RESULTS.md` (запросов / страниц по порталам / карточек exact /
  из них реально в городе Валенсия).

---

## Этап 0 — Быстрые фиксы (1–2 дня)

- [x] **0.1 Миграция `031_task_draft_steps.sql`**: расширить CHECK `user_task_drafts.step`
  значениями `ask`, `target`.
  Файлы: `bot/services/db/migrations/031_*.sql`, README (список миграций).
  Проверка: `tests/test_campaign_postgres.py` — новый тест сохраняет черновик со `step='ask'`.
- [x] **0.2 `task_kind`**: проверять `land → house → commercial → apartment → room`;
  `room` только как отдельное слово без числа перед ним («2 rooms» ≠ «room»).
  Файл: `bot/web_search/queries.py:468`.
  Проверка: тест «apartment Valencia 2+ rooms» → `apartment`; «комната в Мадриде» → `room`.
- [x] **0.3 Бюджет и комнаты в запросах**: `portal_query` и `TemplateQueryGenerator`
  добавляют `hasta <max_price>` / `<rooms> habitaciones` (es), `under <max_price>` (en) и т. д.
  Файл: `bot/web_search/queries.py:343, 478`.
- [x] **0.4 Город ≠ регион**: в `QueryTask` поле `place_level` (city|province|region);
  для `city` аллиасы из `geo.REGIONS` не считаются «на месте»; к запросу добавляется страна
  («Valencia España» / «Valencia Spain»); результаты с маркерами другой страны
  (Carabobo, Venezuela, «, CA», USD для ES) отбрасываются до очереди.
  Файлы: `bot/campaign/geo.py`, `bot/web_search/queries.py:277`, `bot/web_search/worker.py:239`.
- [x] **0.5 Порталы по типу задачи**: `portals()` возвращает список под `task_kind`:
  квартиры/дома → idealista, fotocasa, habitaclia, pisos, yaencontre, kyero, thinkspain, milanuncios;
  земля → terrenos, sareb, idealista, fotocasa; банковские — только при словах «банк/embargo/cheap».
  Квота принудительных `site:` в раунде ≤ 1/3. Файл: `bot/web_search/queries.py:164, 318`.
- [x] **0.6 Fail-closed**: без AI-вердикта (лимит, пауза после ошибки, нет ключа) находка идёт
  в `similar`, не `exact`; при заданном бюджете и неизвестной цене → `other`.
  Файлы: `bot/campaign/runner.py:483-499`, `bot/campaign/tolerance.py:230`.
- [x] **0.7 SearXNG глубже**: `pageno` 1..3 (настройка `WEB_SEARCH_PAGES_PER_QUERY`),
  `WEB_SEARCH_RESULTS_PER_QUERY=30`; убрать 72-часовой запрет повторного запроса для другой
  кампании; TTL 7 дней для `web_seen_urls` страниц вида `index`.
  Файлы: `bot/web_search/searxng.py:50`, `bot/web_search/store.py:236, 270`.
- [x] **0.8 `.env.example`**: `CAMPAIGN_RELEVANCE_MAX_CALLS=2000`, `WEB_SEARCH_MAX_PAGES_PER_HOST=100`,
  `WEB_SEARCH_MAX_LINKS_PER_INDEX=40`, `WEB_SEARCH_MAX_QUERIES_PER_CAMPAIGN=80`,
  `WEB_SEARCH_MAX_PAGES_PER_CAMPAIGN=400`. Лимит рендеров хранить в БД, не в памяти.
- [ ] **0.9 Золотой тест**: `tests/golden/` — 10 задач с ожидаемыми `task_kind`, порталами и
  обязательными словами в запросах. Запускается в CI.

## Этап 1 — Доступ к порталам (3–5 дней)

- [ ] **1.1 Пробник**: запустить `scripts/portal_probe.py` по всем 20 порталам с VPS,
  результат в `docs/PORTALS.md` (портал · статус · слой, который читает · JSON-источник).
  Сюда же внести список обязательных сайтов клиента.
- [x] **1.2 Слой A — структурированные источники**: модуль `bot/web_search/sources/`:
  Idealista API (`IDEALISTA_API_KEY/SECRET`, OAuth), JSON-эндпоинты Fotocasa/Habitaclia,
  JSON-LD `ItemList` с индексных страниц **сохраняется** как находки (цена, м², комнаты).
  Файл-точка: `bot/web_search/worker.py:333`.
- [x] **1.3 Слой B — httpx → `curl_cffi`** с `impersonate="chrome"`, браузерный UA,
  полный набор Accept-заголовков, `Accept-Language` по стране; `WEB_SEARCH_PROXY_URL`
  поддерживает список прокси с ротацией (резидентные/ISP).
  Файл: `bot/web_search/fetcher.py:91`.
- [x] **1.4 Слой C — браузер на 403**: `render.py` вызывается и для 403/429, не только для
  пустого текста; профиль `web-search-render` с stealth (Camoufox или playwright-stealth).
  Для DataDome — опциональный `SCRAPE_API_URL/KEY` (Zyte / ScraperAPI / Bright Data) как
  последний слой. Файлы: `bot/web_search/render.py`, `bot/web_search/worker.py:277, 323`.
- [x] **1.5 Блокировка хоста по слоям**: `web_hosts.blocked_until` → `(host, layer)`;
  403 на слое B переводит хост на слой C, а не блокирует на 12 ч.
  Файл: `bot/web_search/store.py:385`.
- [x] **1.6 Второй поисковый бэкенд**: `SearchBackend` протокол; реализации `SearxngClient`
  и `GoogleCseClient` (или SerpAPI). Запрос уходит в оба, результаты сливаются по `url_key`.
  Файл: `bot/web_search/searxng.py` → `bot/web_search/search_backends.py`.
- [x] **1.7 Классификация URL**: убрать «6 цифр = объявление»; для неизвестных хостов
  объявление = есть JSON-LD `RealEstateListing/Offer` или цена+м² в тексте.
  Файл: `bot/web_search/urls.py:137`.

## Этап 2 — Интервьюер (grill-me) (3–4 дня)

- [x] **2.1 `TaskSpec`** (pydantic, `bot/campaign/spec.py`) + миграция `035_campaign_specs.sql`
  (JSONB на кампании): `hard`, `soft`, `exclude`, `sources`, `delivery`, `investor`.
- [x] **2.2 Агент `bot/control_plane/interviewer.py`** на `OPENROUTER_INTERVIEW_MODEL`
  (Fable/Opus): по одному вопросу, пока все hard-поля не заполнены или помечены «не важно»;
  без лимита 3 вопросов; максимум 10 раундов; кнопка «Хватит, ищи».
  Дерево вопросов по режиму — в `docs/INTERVIEW_TREE.md`.
- [x] **2.3 Карточка ТЗ**: структурированный summary с кнопками «Запустить / Изменить <поле> /
  Отмена»; правка одного поля без сброса задачи. Файл: `bot/control_plane/intake.py:474, 616`.
- [x] **2.4 Голос**: транскрипт показывается; непонятные слова подтверждаются вопросом.
- [ ] **2.5 `user_preferences`**: память прошлых ответов (валюта, язык, город по умолчанию).
- [x] **2.6 Тесты**: сценарии «сразу всё сказал» → 0 вопросов; «только город» → цепочка
  вопросов; инвестор без тикета → вопрос о тикете.

## Этап 3 — Архитектор и сборщики (4–6 дней)

- [ ] **3.1 `SearchPlan`** от LLM (`bot/campaign/architect.py` → `plan_with_model`):
  сайты по приоритету, запросы по языкам с ценой/комнатами/районами, URL-слаги порталов
  (например idealista `/venta-viviendas/valencia-valencia/con-precio-hasta_200000,de-dos-dormitorios/`),
  стоп-критерий. Детерминированный `plan_campaign` остаётся фолбэком.
- [ ] **3.2 Сборщик на Sonnet**: `bot/agents/extraction.py` → `ListingFacts` с цитатами;
  модель `OPENROUTER_EXTRACT_MODEL=anthropic/claude-sonnet-5-5`.
- [ ] **3.3 Дедуп объектов**: `bot/campaign/dedup.py` — ключ (район/адрес + цена ±2 % +
  м² ±3 % + комнаты); кластер → одна карточка со всеми ссылками.
- [ ] **3.4 Статус пользователю**: «сайт · слой · прочитано / найдено».

## Этап 4 — Рецензент и финалист (3–4 дня)

- [ ] **4.1 Рецензент (Opus)**: `bot/agents/reviewer.py` — матрица hard-критериев
  `pass/fail/unknown` с цитатой на каждый; `unknown` по hard → не `exact`.
  Заменяет связку `tolerance.py` + `relevance.py`; ±10 % становится параметром `TaskSpec`.
- [ ] **4.2 Финалист (Fable)**: `bot/campaign/final_report.py` — ранжирование, отчёт
  пользователю: найдено / отклонено по причинам / какие сайты не прочитались и почему.
- [ ] **4.3 Боевой `reduction-worker`**: снять `mode == "shadow"`, отправка через Recorder-outbox;
  старый `analysis-worker` остаётся для постов Facebook без URL.
- [ ] **4.4 Метрики**: таблица `campaign_metrics` + `/campaign report <id>`.

## Этап 5 — Режим «Инвесторы» (4–5 дней)

- [ ] **5.1 `TaskSpec.investor`**: кто, тикет min/max, класс актива, доходность, география,
  язык, роль пользователя (ищем деньги / ищем куда вложить).
- [ ] **5.2 Обогащение**: после сниппета открывать публичные страницы (LinkedIn company,
  сайты фондов, t.me-каналы) тем же фетчером; контакты, описание, последняя активность.
- [ ] **5.3 Скоринг 0–100** по `TaskSpec` и дедуп человека между платформами.
- [ ] **5.4 Отдельные группы** в выдаче: инвесторы / фонды / агентства / девелоперы;
  убрать `startup`, `capital` из ключей недвижимости.
- [ ] **5.5** `CAMPAIGN_COMMENT_LEADS=investors` по умолчанию.

## Этап 6 — Уборка (2 дня)

- [ ] **6.1** Удалить или перенести в `legacy/`: `bot/main.py`, `bot/services/search/`,
  `bot/services/parser/`, `google_maps.py`, `searxng/settings/` (второй конфиг), сервис `worker`.
- [ ] **6.2** Обновить `README.md`, `docs/CAMPAIGNS.md`, `docs/WEB_SEARCH.md` под новую схему.
- [ ] **6.3** CI: `ruff format --check`, золотые тесты, `make check-imports`.

---

## Модели (env)

| Переменная | Значение | Роль |
|---|---|---|
| `OPENROUTER_INTERVIEW_MODEL` | `anthropic/claude-opus-5-5` | интервьюер |
| `OPENROUTER_PLAN_MODEL` | `anthropic/claude-opus-5-5` | архитектор |
| `OPENROUTER_EXTRACT_MODEL` | `anthropic/claude-sonnet-5-5` | сборщик |
| `OPENROUTER_REVIEW_MODEL` | `anthropic/claude-opus-5-5` | рецензент |
| `OPENROUTER_FINAL_MODEL` | `anthropic/claude-opus-5-5` (или Fable, если доступен в OpenRouter) | финалист |
| `OPENROUTER_INTAKE_MODEL` | `anthropic/claude-haiku-4-5` | разбор ответов |

## Критерии приёмки всего плана

1. Задача «квартира в Валенсии до 200k, 2+ комнаты»: ≥ 30 exact-карточек, ≥ 90 % из них —
   город Валенсия, цена ≤ 220k, комнат ≥ 2; Idealista и Fotocasa дают ≥ 10 карточек каждая.
2. Бот задаёт вопросы до заполнения всех hard-полей; «Изменить» правит одно поле.
3. Повторный запуск той же задачи через день даёт новые объявления, а не 0.
4. Финальный отчёт объясняет каждую группу отклонённых и каждый непрочитанный сайт.
5. Инвесторская задача выдаёт людей/компании со скорингом и контактами, разбитые по типам.
