# Технический анализ: архитектура бота и сбор данных из внешних источников

Срез кода: ветка `main` после merge #48 (миграции до `041_web_verification.sql`).
Пути указаны от корня репозитория. «Не обнаружено» означает, что в коде этого нет.

---

## 1. Общая архитектура бота

### 1.1 Компоненты (сервисы `docker-compose.yml`)

| Сервис | Пакет / точка входа | Роль |
|---|---|---|
| `telegram` | `bot/control_plane/main.py`, `service.py` | Telegram-бот на aiogram 3: роли и доступ (`access.py`), интервьюер (`interviewer.py`), голос через OpenRouter Whisper (`stt.py`), карточка ТЗ, диспетчер Orchestra (`bot/orchestra/dispatcher.py`) |
| `campaign-runner` | `bot/campaign/runner.py` (`main()`, стр. 1262) | Ведёт кампании. Внутри работают параллельные циклы: `CampaignRunner` (Facebook-окна, поток карточек, отчёт), `WebSearchWorker` (сайты), `SocialSearchWorker`, `ReachWorker` (инвесторы), `CommentLeadWorker` |
| `searxng` | `docker/searxng/settings.yml` | Внутренний метапоиск, отдаёт только JSON, наружу не открыт |
| `browser` | `bot/browser_session/manager.py` | Менеджер Playwright-сессий Chromium: аренда профиля через Redis и flock, единственный примитив навигации `snapshot()` |
| `facebook-runner` | `bot/facebook_collector/runner.py` | Читает пакеты групп Facebook |
| `analysis-worker` | `bot/analysis_pipeline/main.py` | `collected_posts` → фильтры → LLM-анализ → `findings` |
| `scrapling-worker` | `bot/scrapling_connector/worker.py` | Разовое чтение одной страницы по `/run website` (к кампаниям не относится) |
| `agent-reach-worker` | `bot/agent_reach/worker.py` | Разовое чтение Instagram, TikTok, Facebook по `/run` |
| `verification` | `bot/verification/main.py` | Mini App для человеческой проверки: живой браузер, «Готово», Resume |
| `reduction-worker` | `bot/agents/reduction.py` | Теневая схема Claude + Jev. По умолчанию выключена, ничего не отправляет |
| `postgres`, `redis`, `caddy` | — | БД, аренды и блокировки, единственный HTTPS-вход |

### 1.2 Поток обработки запроса

```
Telegram (текст/голос)
  → control_plane/intake.py + interviewer.py: заполняется TaskSpec (bot/campaign/spec.py), один вопрос за ход
  → карточка ТЗ, кнопка «Запустить»
  → команда /campaign в очереди Orchestra (PostgreSQL, bot/orchestra/dispatcher.py → process_once)
  → architect.plan_campaign() (детерминированный CampaignPlan, bot/campaign/architect.py:202)
  → campaigns.spec + campaigns.plan в БД
  ── campaign-runner (параллельно) ───────────────────────────────────────────
  │ WebSearchWorker.tick() → step() → _advance()       (bot/web_search/worker.py)
  │   1) _new_round: SearchPlan от LLM (один раз), portal_urls → очередь, запросы раунда
  │   2) _search: SearXNG / Google CSE / SerpAPI → кандидаты URL → web_campaign_urls
  │   3) _read_pages → _read: HTTP → браузер → scrape API
  │   4) store.finish_fetch → INSERT collected_posts (как пост Facebook)
  │ Facebook-окна, соцсети, охват инвесторов → тоже collected_posts / reach_contacts
  ────────────────────────────────────────────────────────────────────────────
  → analysis-worker: filter_evidence → OpenRouter (Sonnet, analysis-v6) → findings
  → CampaignRunner._stream (runner.py:556): tolerance.classify → рецензент → dedup → _send_card
  → по завершении: _summary, _final_report (final_report.FinalReporter)
```

Связь между этапами идёт только через PostgreSQL. Веб-этап не вызывает анализ напрямую: он пишет строку в `collected_posts`, а `analysis-worker` забирает её своим циклом.

### 1.3 Технологии

- Python 3, asyncio. Telegram — **aiogram 3.23**. Модели и настройки — **pydantic 2 / pydantic-settings**.
- HTTP: **httpx 0.28** (http2, socks), **curl_cffi ≥ 0.15** (TLS-отпечаток браузера).
- Парсинг: stdlib `HTMLParser` (`bot/web_search/extract.py`), **Scrapling 0.4.8** (только `Selector`, без `fetchers`) для JSON-LD и OpenGraph (`structured.py`).
- Браузер: **Playwright 1.63**, Chromium в режиме headful. Persistent-профили, флаги `--disable-blink-features=AutomationControlled` и без `--enable-automation`.
- БД: **PostgreSQL** через **asyncpg**, 41 SQL-миграция. Очереди сделаны на таблицах PostgreSQL с арендами и `on conflict`.
- **Redis** — только аренды браузерных профилей и блокировки. Как брокер задач не используется.
- Поиск: **SearXNG** (self-hosted), опционально **Google CSE** и **SerpAPI**.
- LLM: всё через **OpenRouter**, по одной переменной `OPENROUTER_*_MODEL` на роль.
- Celery, RQ, Kafka и подобные брокеры **не обнаружены**: каждый воркер — цикл `serve(poll_seconds)` над таблицами.

---

## 2. Модули сбора данных

### 2.1 Кто отвечает за поиск и парсинг объявлений

Весь сбор с сайтов недвижимости — пакет **`bot/web_search/`**, запускается в `campaign-runner` через `_web_stage()` (`bot/campaign/runner.py:1337`).

| Файл | Класс / функция | Что делает |
|---|---|---|
| `worker.py` | `WebSearchWorker`, `WebSearchConfig` | Оркестратор этапа: раунды запросов, поиск, очередь URL, слои чтения, человеческая проверка |
| `searxng.py` | `SearxngClient`, протокол `Searcher`, `SearchHit`, `SearchError` | Запросы в SearXNG |
| `search_backends.py` | `GoogleCseClient`, `SerpApiClient`, `MergedSearcher`, протокол `SearchBackend` | Дополнительные поисковики и слияние выдачи |
| `queries.py` | `QueryTask`, `OpenRouterQueryGenerator`, `TemplateQueryGenerator`, `FallbackQueryGenerator`, `cover_portals`, `portal_query` | Генерация запросов (LLM или шаблоны), обязательный `site:` по порталам |
| `fetcher.py` | `PageFetcher`, `HttpxTransport`, `CurlTransport`, протокол `Transport` | Слой 1: HTTP GET с robots.txt, паузами, прокси и impersonate |
| `render.py` | `BrowserRenderer`, протокол `Renderer`, `ChallengeDetected` | Слой 2: чтение через сервис `browser` |
| `scrape_api.py` | `ScrapeApiClient`, протокол `Scraper` | Слой 3: внешний unlocker API |
| `extract.py` | `parse_html`, `listing_links`, `looks_like_index`, `post_text` | Текст, ссылки и распознавание индексной страницы |
| `structured.py` | `structured`, `from_jsonld`, `Structured`, `facts_block` | JSON-LD и OpenGraph → плоский dict объявления |
| `urls.py` | `PORTALS`, `SPAIN_PORTALS_BY_KIND`, `classify_url`, `fetchable`, `deal_conflict`, `listing_evidence` | Правила порталов: что считать объявлением, что списком, что отбрасывать |
| `store.py` | `WebStore` (протокол), `PostgresWebStore` | Очередь, глобальный дедуп URL, блокировки хостов, запись в `collected_posts` |
| `settings.py` | `WebSearchSettings` | Все переменные `WEB_SEARCH_*` |

Остальные коллекторы — не порталы недвижимости: `bot/facebook_collector` (группы FB), `bot/social_search` (TikTok, Instagram, LinkedIn через браузер), `bot/campaign/reach.py` + `people.py` (поисковая выдача по инвесторам), `bot/scrapling_connector` (одна страница по `/run website`). Директория `legacy/` — старый бот, в стек не входит.

### 2.2 Поддерживаемые источники

Отдельных парсеров под конкретные порталы **нет**. Используется один общий конвейер, а для известных порталов заданы URL-шаблоны и списки.

**С URL-регэкспами** (`urls.PORTALS`, по ним различаются объявление и список): idealista.com, fotocasa.es, pisos.com, milanuncios.com, habitaclia.com, yaencontre.com, kyero.com, thinkspain.com, spainhouses.net, solvia.es, servihabitat.com, tucasa.com, enalquiler.com. Украина: dom.ria.com, lun.ua, olx.ua, rieltor.ua.

**Только в списках обхода** (`SPAIN_PORTALS`, `SPAIN_PORTALS_BY_KIND`, `SPAIN_BANK_PORTALS`), без регэкспа: **terrenos.es**, indomio.es, sareb.es, alisedainmobiliaria.com, altamirainmuebles.com, haya.es, hogaria.net, green-acres.es. Для них `classify_url` работает по общему правилу `_generic_listing` (слово объявления плюс id из 5+ цифр). Из-за этого **Terrenos.es распознаётся хуже**, и `search_result()` (карточка из сниппета) для него не срабатывает: эта функция требует `portal_listing()`, то есть регэксп портала.

Порядок по типу объекта (`SPAIN_PORTALS_BY_KIND`):
- `apartment` / `house`: idealista, fotocasa, habitaclia, pisos, yaencontre, kyero, thinkspain, milanuncios, indomio, tucasa, spainhouses;
- `land`: idealista, fotocasa, **terrenos.es**, sareb, milanuncios, pisos, kyero;
- `commercial`, `room` — свои списки. Банковские порталы добавляются только по словам «banco / embargo / barato / дешев…».

Прямые URL поиска с фильтрами пишет LLM в `SearchPlan.portal_urls`. Примеры шаблонов в промпте (`bot/campaign/search_plan.py:311-314`) есть только для idealista и fotocasa.

**Поисковые системы:** SearXNG (внутренние движки настроены в `docker/searxng/settings.yml`), Google Programmable Search (`GOOGLE_CSE_*`), SerpAPI (`SERPAPI_API_KEY`). Google, Bing и DuckDuckGo напрямую не опрашиваются, только через SearXNG.

### 2.3 Как выполняется запрос к источнику

Ни у одного портала нет собственного API-клиента. Для всех путь одинаковый.

1. **Поиск** (`WebSearchWorker._search`, `worker.py:350`): запрос с `site:idealista.com …` уходит в `Searcher.search()`. Выдача фильтруется: `fetchable`, `deal_conflict`, `listing_evidence` для неизвестных хостов, `geo.foreign_tld`, `geo.foreign_markers_hit`. Затем кандидаты встают в очередь через `store.enqueue`.
2. **Прямые страницы поиска** из плана (`_enqueue_portal_urls`, `worker.py:340`) ставятся в очередь как индексные страницы с `depth=0`.
3. **Чтение** (`_read`, `worker.py:514`):
   - **Слой 1, HTTP:** `PageFetcher.fetch()` (`fetcher.py:336`). По умолчанию это `CurlTransport` (curl_cffi, `impersonate=chrome`, UA Chrome 124, `Sec-Fetch-*`, `Accept-Language` по стране). При `WEB_SEARCH_IMPERSONATE=off` используется `HttpxTransport`. Cookies не сохраняются, каждый запрос идёт в свежей сессии.
   - **Слой 2, браузер:** `BrowserRenderer.render()` → `BrowserSessionClient` → `manager.snapshot(platform="website")`. Возвращает текст, ссылки, JSON-LD и `frames`. Профиль `web-search-render` без логина.
   - **Слой 3, scrape API:** `ScrapeApiClient.fetch()`, запрос вида `GET {WEB_SEARCH_SCRAPE_API_URL}?url=<page>`, при заданном ключе с `Authorization: Bearer <key>`.
4. **Разбор:** `_page()` (`worker.py:650`). Индексная страница даёт дочерние ссылки (`depth=1`), сначала из JSON-LD `ItemList`, потом из HTML. Объявление даёт текст, перед которым ставится строка `JSON-LD: {...}` из `structured()`.

Selenium **не обнаружен**. Playwright используется только в сервисе `browser`; веб-этап обращается к нему по HTTP API.

### 2.4 Разделение на обычный HTTP и browser/stealth

Разделение **есть**, это слои, и решение принимается на каждый URL:

- HTTP пробуется первым, если слой открыт для хоста (`store.layer_state(host)`).
- Браузер включается в двух случаях: (а) HTTP 200 без текста или индекс без ссылок (`_wants_render` → `_render_empty`); (б) отказ HTTP из `RENDER_ON = ("http_403","http_429","http_503","captcha")` (`worker.py:770`) → `_next_layers` → `_render_refused`.
- Scrape API — только после отказа обоих слоёв, только для объявлений и ссылок с `depth ≥ 1`. **Индексные страницы через scrape API не идут никогда** (`_may_fall_back`, `worker.py:497`).

Настоящего stealth нет. Есть только curl_cffi-импersonation на HTTP-слое и снятие флагов автоматизации в браузере. Camoufox, playwright-stealth, ротация отпечатков и прокси на уровне браузера **не обнаружены**: в `bot/browser_session` нет ни одного упоминания proxy. Пункт PLAN.md 1.4 упоминает stealth, но в коде его нет.

### 2.5 Обработка 403, 429, капч и блокировок

| Ситуация | Где | Поведение |
|---|---|---|
| HTTP ≥ 400 | `PageFetcher._read` | `FetchError("http_<code>")`, без повторов |
| Ошибка соединения с прокси | `PageFetcher._once` | Пробуется следующий прокси из списка (только `ConnectFailed`) |
| Короткая страница со словами captcha / datadome / «are you a robot» / «access denied» | `looks_blocked()` `worker.py:773` | Считается `captcha` |
| 403 / 429 / 503 / captcha на HTTP | `_read` → `_next_layers` | Браузер, затем scrape API |
| Капча в браузере, проверка выключена | `_render_refused` | `render_blocked` |
| Капча в браузере, `WEB_SEARCH_HUMAN_VERIFICATION=on` | `classify_website` → `ChallengeDetected` → `_defer` | Задача verification, сайт на паузе. После прохождения человеком сайт читается тем же профилем: не больше 40 страниц, пауза 8 с |
| 3 отказа подряд на слое | `_count_refusal` (`store.py:682`), `REFUSALS_TO_BLOCK=3`, `BLOCK_HOURS=12` | Слой хоста блокируется на 12 ч **глобально для всех кампаний** (`web_hosts.http_blocked_until` / `render_blocked_until`) |
| Все слои заблокированы | `begin_fetch` → `HOST_BLOCKED` | Сайт не запрашивается, остаётся карточка из сниппета поиска (`search_result`, `worker.py:779`) |
| robots.txt запрещает | `PageFetcher.allowed` | Ни один слой страницу не читает, остаётся только сниппет |
| Поисковик вернул 402/403/429 | `search_backends._check` | `SearchError("quota")`, бэкенд пропускается на этот запрос |

Автоматическое решение капч **не обнаружено**, это сделано намеренно (см. `docs/WEB_SEARCH.md`, «Проверка сайта человеком»).

---

## 3. Точки интеграции

### 3.1 Существующие абстракции

| Протокол | Файл | Сигнатура | Подходит для |
|---|---|---|---|
| `Searcher` / `SearchBackend` | `searxng.py:32`, `search_backends.py:17` | `async search(query, *, language, pages) -> list[SearchHit]` | Новый поисковик (SerpAPI-подобные) |
| `Transport` | `fetcher.py` | `open(url, headers, proxy)` → контекст с `TransportResponse` | Другой HTTP-клиент для слоя 1 |
| `Renderer` | `render.py` | `async render(url) -> RenderedPage` | Другой браузерный бэкенд (облачный браузер) |
| `Scraper` | `scrape_api.py:21` | `async fetch(url) -> FetchedPage` | **Любой unlocker «URL → HTML»** (Scrape.do, ScraperAPI, Zyte, Bright Data) |
| `QueryGenerator` | `queries.py:275` | `generate(task, used, count)` | Генерация запросов |
| `SearchPlanner` | `bot/campaign/architect.py` | LLM-план | — |
| `WebStore` | `store.py:66` | Очередь, тикеты, `finish_fetch` | Запись результатов |

Абстракции **«источник структурированных объявлений»** (вход — фильтры, выход — список объявлений) **нет**. Все источники в коде устроены как «URL → HTML». Каталог `bot/web_search/sources/`, о котором говорит PLAN.md 1.2 (`IDEALISTA_API_KEY/SECRET`, JSON-эндпоинты Fotocasa/Habitaclia), **не существует**, хотя пункт отмечен `[x]`. Из него реально сделано только сохранение JSON-LD `ItemList` с индексных страниц (`index_cards`, `worker.py:802`). Переменные `IDEALISTA_*` и `APIFY_*` в коде и в `.env.example` **не обнаружены**.

### 3.2 Как передаются параметры поиска

1. `TaskSpec` (`bot/campaign/spec.py:427`, pydantic) содержит `place` (name, country, level, districts, radius_km), `deal`, `property_type`, `budget: Money(min,max,currency)`, `rooms: Range`, `area: Range`, `must_have`, `exclude`, `sources(required/extra/blocked)`, `deviations`. Хранится в `campaigns.spec` (JSON).
2. `architect.plan_campaign` / `plan_with_model` строит `CampaignPlan` (`bot/campaign/models.py`) с полями `location`, `location_aliases`, `country` и `constraints: dict`. Разрешённые ключи `constraints` (`CONSTRAINT_KEYS`): `deal, max_price, min_price, rooms, min_area, max_area, property_type, districts, currency`. Заполнение — `architect.py:294-311`.
3. Для веб-этапа `query_task(campaign)` (`worker.py:156`) собирает `QueryTask` (`queries.py:171`): `location`, `constraints`, `country_code`, `place_level`, `search_plan`, `blocked_hosts`.
4. Для фильтрации после анализа `tolerance.request_for(constraints, …)` (`tolerance.py:261`) строит `Request`.

Структурированного объекта «фильтры портала» нет. Сейчас фильтры попадают к порталу только двумя путями: текстом запроса (`hasta 200000`, `2 habitaciones`) и через URL, который написала LLM в `portal_urls`.

### 3.3 Формат данных от парсеров

- Поиск: `SearchHit(url, title, snippet, engine)` — dataclass.
- Чтение страницы: `FetchedPage(url, html)` / `RenderedPage(url, title, text, links, jsonld, frames)` — dataclass.
- Структура объявления: `dict` из `structured._listing()` с ключами `title, url, price, currency, area_m2, rooms, address, property_type, deal, description`.
- Результат чтения: `PageResult(ok, kind, final_url, title, text, error, via, layer)` — dataclass (`models.py`).
- **Граница с остальной системой — свободный текст:** `collected_posts.body_text`, где первая строка `JSON-LD: {...}`, а дальше видимый текст. `raw_payload` (jsonb) хранит служебные метаданные (`_raw_payload`, `store.py:161`).
- После анализа: `AnalysisResult` (pydantic, `bot/analysis_pipeline/models.py:9`) с полями `price_amount, price_currency, deal_type, property_type, rooms, area_m2, location, district, address, floor…`. Это `findings.structured_payload` — его читают `tolerance`, `dedup` и `final_report`.

Значит, внешний источник может передать в систему **точные числа**: для этого достаточно записать их строкой `JSON-LD: {...}` в начало `body_text`. Анализ (`OPENROUTER_ANALYSIS_MODEL`, промпт в `openrouter.py`) уже настроен считать цифры сайта главнее текста.

---

## 4. Обработка и фильтрация результатов

### 4.1 Дедупликация (три уровня)

1. **URL, глобально.** `web_seen_urls.url_key` — SHA-256 нормализованного URL (`urls.url_key` → `bot/utils/urls.normalize_url`: без `www.`, фрагмента и `utm_*`, query отсортирован, http приведён к https). `begin_fetch` (`store.py:426`) захватывает URL через `insert … on conflict`. Объявление читается **один раз для всей системы**. Индексные страницы перечитываются через `WEB_SEARCH_INDEX_TTL_DAYS`. Внутри одного ответа поиска `MergedSearcher` дополнительно убирает дубли по `url_key`.
2. **Пост:** `collected_posts … on conflict do nothing` плюс `content_hash`.
3. **Объект на разных сайтах:** `bot/campaign/dedup.py`, `same_object(a, b)` (стр. 282). Цена ±2 %, площадь ±3 %, комнаты, этаж, номер дома, общие токены улицы или района, шинглы текста, телефон. Вызов — `CampaignRunner._deduplicate` (`runner.py:597`). Дубликат не получает новой карточки: к первой дописывается «Также на: …» (`attach_to_cluster`, миграция 036).

Запросы тоже дедуплицируются: `queries.dedupe` / `similar` (совпадение токенов ≥ 75 %), `web_search_queries`.

### 4.2 Фильтры после сбора

По порядку:
- **До очереди** (`_search`): блок-листы `BLOCKED_HOSTS` / `NON_LISTING_HOSTS` / `NON_LISTING_PATHS`, конфликт сделки по пути URL, `listing_evidence` для неизвестных сайтов, чужой TLD, маркеры другой страны («Valencia, Venezuela»).
- **При чтении:** `MIN_POST_CHARS=120`. Для неизвестного URL `classify_page` требует JSON-LD или цену с площадью. Лимиты на хост: `max_pages_per_host`, `max_pages_per_unknown_host=5`.
- **Анализ:** `filter_evidence` (`analysis_pipeline/filters.py:102`): длина, давность 90 дней, спам, ключевые слова вертикали. Затем LLM → `AnalysisResult`, `relevant` и `category`.
- **Правила задачи:** `tolerance.classify` (`tolerance.py:308`) раскладывает находки по корзинам `exact / similar / other / excluded`. Проверяются: не предложение (`not_an_offer`), сделка, тип, страна и город (`foreign`, `_nearby_hit`), мин/макс площадь, комнаты, мин. цена, бюджет ±10 % (`BUDGET_TOLERANCE`) и согласованные отступления из `TaskSpec.deviations`. Неизвестная цена при заданном бюджете даёт не `exact`.
- **Рецензент:** `bot/agents/reviewer.py`, `hard_criteria(campaign)`. По каждому критерию `pass / fail / unknown` с цитатой. Любой `unknown` снимает статус `exact` (fail-closed, `CAMPAIGN_RELEVANCE_FAIL_CLOSED`). Вызов — `_judge` / `_relevance` в `runner.py:668-744`.

### 4.3 Итоговый отчёт

- Каждая карточка — `finding_card()` (`runner.py:1235`), отправка — `_send_card`. Похожие варианты ждут «Одобрить» (`bot/campaign/offers.py`).
- Сводка по источникам — `_summary` (`runner.py:939`), `bot/campaign/summary.py`. Idealista и Fotocasa показываются всегда (`ALWAYS_SHOWN`).
- Итоговый отчёт — `_final_report` (`runner.py:965`) → `FinalReporter.build` (`final_report.py:472`). Содержит `tally` по причинам отказов, `rank_cards` (топ-10 по `card_score`), воронку по сайтам (`store.site_report` → `SiteReport`), `unreadable_sites` и 2–4 рекомендации LLM (`OpenRouterRecommender`) или правил (`fallback_recommendations`). В промпте рекомендаций уже указан рычаг «proxy or the Idealista API» (`final_report.py:338`).
- Метрики — `campaign_metrics` (миграция 039), команда `/campaign report`.

---

## 5. Инфраструктура и ограничения

### 5.1 Прокси, пулы, очереди

- **Прокси:** `WEB_SEARCH_PROXY_URL`, один адрес или список через запятую, http(s) или socks5. Каждый хост закреплён за одним прокси (`pick_proxies`: `crc32(host) % n`), следующий берётся только при ошибке соединения. Работает **только для HTTP-слоя и SearXNG**. В `docker-compose.yml` есть закомментированный `gluetun` (VPN). Для браузера прокси **не обнаружены**.
- **Браузерный пул:** одного профиля `web-search-render` на всю систему достаточно, потому что `BrowserRenderer` арендует его на одну страницу (Redis-аренда, `lease_seconds=120`). Параллельного пула для веб-этапа нет: страницы рендерятся последовательно. Пока человек держит профиль на проверке, браузер для веба закрыт (`verification_busy`).
- **Очереди:** таблицы PostgreSQL — `web_search_queries`, `web_campaign_urls`, `web_seen_urls`, `orchestra`-инбокс, `acquisition_batches` / `acquisition_runs`. Кампанию ведёт один воркер (`take_lease`); зависшие чтения освобождаются через `store.recover`.

### 5.2 Rate limit, кэширование, повторы

| Механизм | Где | Значение по умолчанию |
|---|---|---|
| Пауза на хост | `PageFetcher._pace` | `WEB_SEARCH_HOST_INTERVAL_SECONDS=5`, учитывает `Crawl-delay` (≤ 30 с) |
| Один запрос к хосту одновременно | `PageFetcher._lock` | asyncio.Lock на хост, в памяти процесса |
| Кэш robots.txt | `_Robots` | 12 ч; при недоступности robots.txt — запрет на 10 мин |
| Лимиты кампании | `WebSearchConfig` / `settings.py` | 80 запросов, 400 страниц, 100 на хост, 60 рендеров, 40 scrape API, 240 мин |
| Суточные лимиты | `store.usage()` | 3000 страниц, 300 запросов (скользящие 24 ч, в БД) |
| Лимиты Google CSE / SerpAPI | `MergedSearcher._allow` | 90 в сутки, **в памяти**, сбрасываются при рестарте |
| Повторное чтение индекса | `index_ttl_days` | 7 дней |
| Повторы HTTP | — | **Не обнаружены.** Ни ретраев, ни backoff на 429. Повторяются только прокси при ошибке соединения |

### 5.3 Текущие проблемы с блокировками (Idealista)

1. **Idealista защищён DataDome.** HTTP-слой (даже curl_cffi) и Playwright без stealth и без резидентного IP обычно получают 403 или капчу. Это зафиксировано в `docs/REVIEW_AND_PLAN.md` (п. 11, рекомендации 1 и 3) и в разделе «Риски» `docs/WEB_SEARCH.md`.
2. **Три отказа блокируют слой на 12 ч для всех кампаний** (`REFUSALS_TO_BLOCK=3`, `BLOCK_HOURS=12`). На практике Idealista быстро уходит в `HOST_BLOCKED`, и остаются только карточки из сниппетов поисковика: без цены, площади и комнат, то есть почти всегда `similar` или `other`.
3. **Неудачный URL помечается навсегда.** `web_seen_urls` получает `state='failed'`, а `begin_fetch` повторно захватывает только зависшие `fetching` и индексы после TTL. Объявление Idealista, на котором однажды был 403, **больше не будет прочитано ни одной кампанией** — даже после подключения API или прокси. При интеграции это нужно учесть (см. 6.4).
4. **Индексные страницы Idealista не идут в scrape API** (`_may_fall_back`), а именно они дают больше всего ссылок за один запрос.
5. **robots.txt соблюдается всеми слоями.** Пути, которые Idealista закрыл в robots.txt, не прочитает и scrape API: проверка `fetcher.allowed()` стоит раньше выбора слоя (`_read_pages`, `worker.py:411`).
6. **Режим человеческой проверки** (`WEB_SEARCH_HUMAN_VERIFICATION`) выключен по умолчанию и не масштабируется: 40 страниц на одну пройденную капчу, нужен живой человек.
7. **Формат `ScrapeApiClient`** — `Authorization: Bearer`. ScraperAPI и Scrape.do принимают ключ в query (`api_key=` / `token=`). Клиент это выдерживает, если вписать ключ прямо в `WEB_SEARCH_SCRAPE_API_URL` (`https://api.scrape.do/?token=XXX`) и оставить `WEB_SEARCH_SCRAPE_API_KEY` пустым: разделитель `&` подставится, заголовок не отправится. Но дополнительные параметры (`render=true`, `super=true`, `geoCode=es`) тоже придётся вписывать в URL вручную. Отдельной конфигурации для них нет.

---

## 6. Рекомендации по интеграции готовых API

Внешние API бывают двух видов, и подключать их нужно в разные места:

| Тип | Примеры | Вход → выход | Куда |
|---|---|---|---|
| **Unlocker (URL → HTML)** | Scrape.do, ScraperAPI, Zyte API, Bright Data Web Unlocker | URL страницы → HTML | Существующий слой 3 `Scraper` |
| **Структурированный источник (фильтры → объявления)** | Apify Idealista Scraper, официальный Idealista API (OAuth, `/3.5/es/search`) | Город, тип, цена, комнаты → JSON-список | Новый источник в веб-этапе (ниже) |

### 6.1 Самое чистое место для Idealista API

**Вариант A — уже сегодня, без кода (unlocker).**
`WEB_SEARCH_SCRAPE_API_URL=https://api.scrape.do/?token=…&geoCode=es&super=true` (или ScraperAPI с `api_key=…&country_code=es`). Подключение — `settings.scraper()` → `WebSearchWorker(scraper=…)`.
Ограничения из раздела 5.3: только после отказа HTTP и браузера, только объявления (не индексы), 40 на кампанию, robots.txt первым. Минимальная доработка — разрешить scrape API для индексов выбранных хостов: правка `_may_fall_back` (`worker.py:497`) плюс настройка вида `WEB_SEARCH_SCRAPE_API_INDEX_HOSTS=idealista.com`. Отдельный полезный флаг — «для этих хостов сразу scrape API, минуя HTTP и браузер», чтобы не тратить 3 отказа и не блокировать слой.

**Вариант B — рекомендуемый для Idealista (структурированный источник).**
Добавить в веб-этап **источник объявлений**, который вызывается **один раз на кампанию в первом раунде**, рядом с `_enqueue_portal_urls`:

```
bot/web_search/sources/__init__.py
bot/web_search/sources/base.py       # протокол ListingSource + SourceListing (dataclass)
bot/web_search/sources/idealista.py  # IdealistaApiSource (официальный API) и/или ApifyIdealistaSource
```

```python
class ListingSource(Protocol):
    name: str                      # "idealista_api"
    hosts: frozenset[str]          # {"idealista.com"}
    def supports(self, task: QueryTask) -> bool: ...   # страна ES, вертикаль real_estate
    async def search(self, task: QueryTask, *, limit: int) -> list[SourceListing]: ...
    async def aclose(self) -> None: ...

@dataclass(frozen=True)
class SourceListing:
    url: str                       # https://www.idealista.com/inmueble/<propertyCode>/
    title: str
    price: float | None; currency: str | None
    area_m2: float | None; rooms: int | None
    address: str | None; property_type: str | None; deal: str | None
    description: str = ""
```

Точка вызова — `WebSearchWorker._new_round` (`worker.py:304`), ветка `used_count == 0`:

```python
if used_count == 0:
    campaign = await self._with_search_plan(campaign)
    await self._enqueue_portal_urls(campaign)
    await self._from_sources(campaign)          # новое
```

`_from_sources` для каждого `source.supports(task)`:
1. вызывает `source.search(task, limit=…)` с таймаутом;
2. для каждого `SourceListing` строит `QueuedUrl(url, url_key(url), host_of(url), depth=0, kind="listing")`;
3. берёт тикет `store.begin_fetch(..., contact_site=False)`. Так глобальный дедуп `web_seen_urls` работает как обычно, а сайт не запрашивается и не получает отказов;
4. пишет `store.finish_fetch(ticket, PageResult(True, "listing", url, title, text, via="api", layer="api"))`, где
   `text = f"{facts_block(facts)}\n{title}\n{description}\n\nСсылка: {url}"`. Это тот же формат, что у JSON-LD-объявлений, поэтому анализ, `tolerance`, рецензент, дедуп «Также на» и отчёт работают **без изменений**.

Почему именно здесь:
- кампания, ТЗ, план и `QueryTask` уже собраны, все фильтры доступны;
- результат попадает в `collected_posts` тем же путём, что и всё остальное, — ни один потребитель ниже по конвейеру не меняется;
- `url_key` объявления совпадает с URL из выдачи SearXNG, поэтому объявление, найденное через API, не будет прочитано повторно по HTTP (`duplicate`);
- без ключа источник просто не регистрируется. Поведение по умолчанию не меняется.

### 6.2 Нужен ли отдельный воркер или сервис

**Не нужен** ни для варианта A, ни для синхронного API (официальный Idealista API, Apify `run-sync-get-dataset-items`, который отвечает за 30–120 с). Веб-этап и так асинхронный, выполняется по шагам под арендой кампании, а `page_runtime_seconds` и таймаут вызова ограничивают время.

Отдельный цикл или сервис оправдан, только если:
- Apify запускается асинхронно (старт актора → поллинг run → чтение dataset). Тогда нужна таблица `source_runs (campaign_id, source, external_run_id, state)` и неблокирующий опрос в `_advance`, чтобы шаг не висел минуты. Это всё ещё можно делать внутри `WebSearchWorker`: «запустил» в первом раунде, «забрал» на следующих тиках;
- нужен общий кэш выдачи между кампаниями (одинаковые фильтры Валенсии сегодня) — для экономии платных вызовов.

### 6.3 Передача параметров и получение результатов

Сопоставление `QueryTask` с параметрами Idealista API (`POST https://api.idealista.com/3.5/es/search`, OAuth2 client_credentials):

| Источник в коде | Параметр Idealista |
|---|---|
| `constraints["deal"]` (`sale`/`rent`) | `operation=sale|rent` |
| `constraints["property_type"]` / `queries.task_kind(task)` | `propertyType=homes|offices|premises|garages|bedrooms|land` (`land` ← `land`, `homes` ← `apartment/house`) |
| `constraints["max_price"]`, `min_price` | `maxPrice`, `minPrice` |
| `constraints["min_area"]`, `max_area` | `minSize`, `maxSize` |
| `constraints["rooms"]` | `bedrooms` (список 0–4) |
| `task.location` + `geo` | `center=lat,lng` + `distance` (геокодировать город; `spec.place.radius_km` → `distance`), либо `locationId` |
| `country_code == "ES"` | `country=es` (`/3.5/es/…`; есть `pt`, `it`) |
| `spec.deviations.budget_pct` | Расширить `maxPrice` на допуск, чтобы `similar`-варианты тоже пришли |

Для Apify-актора (Idealista Scraper) обычно передаётся `startUrls` с готовым URL поиска. Его уже умеет строить LLM-план (`SearchPlan.portal_urls`, `queries.plan_portal_urls`). Детерминированный построитель URL Idealista по `QueryTask` надёжнее LLM, и его стоит добавить в `idealista.py` по образцу примера из `search_plan.py:311`.

Ответ нужно приводить к `SourceListing`, а дальше к строке `facts_block()`. Ключи: `price`, `size` → `area_m2`, `rooms`, `address`/`district`/`municipality` → `address`, `propertyType`, `operation` → `deal`, `url`. Анализ получит точные числа через `JSON-LD: {...}`.

### 6.4 Что изменить в существующем коде

Обязательно для варианта B:
1. **`bot/web_search/models.py`:** в `PageResult.via` добавить `"api"`, в `layer` — `"api"`. `WebProgress.layer` уже знает `api`.
2. **Миграция `042_listing_sources.sql`:** расширить CHECK `web_campaign_urls.layer` значением `'api'` (сейчас `'http','render','scrape','none'`, миграция 039) и при необходимости добавить `acquisition_runs.acquisition_method` (CHECK в `003_orchestration.sql`, сейчас `begin_fetch` пишет `'scrapling'`). Добавить счётчик вызовов источника на кампанию и в сутки (по образцу `mark_scraped` / `scrapes_used`), **в БД, а не в памяти**, потому что вызовы платные.
3. **`bot/web_search/store.py`:**
   - в `finish_fetch` для `via="api"` не трогать счётчики отказов хоста (`contacted=False`): сейчас это следует из `layer == "none"`, нужно расширить условие;
   - в `begin_fetch` разрешить повторный захват `failed`-объявления, если новая попытка идёт через API (иначе пункт 3 из 5.3 не даст взять объявления, на которых раньше был 403). Например, `or (web_seen_urls.state = 'failed' and $api)`.
4. **`bot/web_search/worker.py`:** новый параметр `sources: list[ListingSource]` в `WebSearchWorker.__init__`, метод `_from_sources`, вызов в `_new_round`. В `WebSearchConfig` — `max_source_listings_per_campaign`.
5. **`bot/web_search/settings.py`:** `IDEALISTA_API_KEY`, `IDEALISTA_API_SECRET` (или `APIFY_TOKEN`, `APIFY_IDEALISTA_ACTOR`), лимиты, фабрика `sources()`. Секреты держать с `repr=False`, как `scrape_api_key`.
6. **`bot/campaign/runner.py` `_web_stage` (стр. 1337):** передать `sources=settings.sources()` и добавить их `aclose` в `closers`.
7. **`docker-compose.yml`** (блок `environment` у `campaign-runner`) и **`.env.example`:** новые переменные.
8. **`bot/campaign/final_report.py` / `summary.py` / `metrics.py`:** учитывать слой `api` в воронке («Idealista: N из API»), иначе в отчёте будет «прочитано 0».
9. **Тесты** (`tests/`): фейковый `ListingSource` → `collected_posts` содержит `JSON-LD:`, дубль из SearXNG получает `duplicate`, без ключа ничего не меняется. Прогон — `make lint test`.

Желательно:
- `urls.PORTALS`: добавить регэкспы **terrenos.es** (и indomio.es, sareb.es), чтобы работали `portal_listing`, карточки из сниппетов и корректная классификация;
- для варианта A: флаг «хост сразу через scrape API» и scrape API для индексов выбранных хостов (`_may_fall_back`);
- исправить пометку PLAN.md 1.2: она стоит как выполненная, а модуля `sources/` нет.

### 6.5 Итог

- **Быстрый результат без кода:** настроить Scrape.do или ScraperAPI через `WEB_SEARCH_SCRAPE_API_URL` с ключом в URL. Это починит часть объявлений Idealista, но не индексы и не уже помеченные `failed` URL.
- **Правильное решение:** модуль `bot/web_search/sources/` с протоколом `ListingSource`, вызов в `WebSearchWorker._new_round`, запись через `begin_fetch(contact_site=False)` → `finish_fetch` с текстом `JSON-LD: {...}`. Отдельный сервис не нужен. Всё ниже по конвейеру (анализ, правила, рецензент, дедуп, карточки, отчёт) остаётся без изменений, меняются только модели, store, одна миграция, настройки и отчётные счётчики.
