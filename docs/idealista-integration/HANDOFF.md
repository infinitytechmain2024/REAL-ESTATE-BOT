# HANDOFF — состояние интеграции и что доделать

Ветка: `claude/optimistic-feynman-y4ayed` (от `main` @ `06988dc`). Коммиты исходного handoff: `77f6cf8` (пакет + анализ), `c3b535c` (Phase −1), `aebff1e` (handoff).
Тесты Phase −1: `1417 passed, 0 skipped` (вместе с Postgres-тестами), `ruff check bot tests` — чисто. Результаты текущих фаз указаны ниже.

## 1. Что сделано

### Шаг 1–2. Пакет и анализ
- Пакет распакован в `docs/idealista-integration/` (README, PROMPT, PLAN, TECHNICAL_SPECIFICATION, CHECKLIST, AGENTS, SKILLS, agents/, skills/).
- `SCRAPING_ANALYSIS.md`: карта пайплайна с file:line, сверка ТЗ с кодом, диагноз прогона «участок, Мадрид, покупка,
  ≥2000 м²» и read-only SQL для VPS (раздел 5).
- Главные расхождения ТЗ с кодом:
  - `finish_fetch(via=, layer=)` — таких kwargs нет: `via`/`layer` — поля `PageResult`, их Literal-типы надо расширять;
  - `begin_fetch` без параметра `layer`; повторный захват failed URL требует правок в `enqueue` **и** `begin_fetch`
    (и в `MemoryWebStore`);
  - метка `"api"` уже занята: в статусе так называется scrape API (`worker._scrape`, `_set_layer(..., "api")`);
  - `ScrapeApiClient` шлёт ключ как `Authorization: Bearer`, а Scrape.do ждёт параметр `token`;
  - номер миграции Idealista — **043** (042 уже занята).

### Шаг 3. Phase −1 (быстрые фиксы, без новых сервисов)

| Что | Где | Настройка |
|---|---|---|
| Allowlist порталов клиента (20 доменов `urls.SPAIN_PORTALS`) + названные в задаче сайты; словари и научные сайты в чёрном списке | `worker._host_allowed/_allowed_hosts`, `urls.NON_LISTING_HOSTS` | `WEB_SEARCH_DOMAIN_POLICY=strict\|soft\|off` |
| Слово сделки в каждом запросе; фильтр сделки по пути (любой сайт) и по заголовку | `queries.with_deal`, `worker._search`, `urls._RENT_WORDS` | — |
| Отчёт: «площадь не указана» / «не удалось подтвердить» / «ИИ-проверка не сработала» / «бюджет исчерпан» раздельно; ошибки ИИ по кодам; analysis-worker не теряет посты молча, при 401/402 останавливает пакет | `final_report.py`, `relevance.CATEGORY`, `runner._judge`, `analysis_pipeline/main.py` | — |
| Парсер площади и сделки (испанские числа, ha, сотки, parcela vs construida) | `bot/utils/listing_text.py`; `structured._listing` (`plot_m2`); `openrouter._number_present`; `tolerance.min_area_of` | — |
| Circuit breaker по сайту (включая платный scrape API) | `worker._note_read`, `HOST_BREAKER` | `WEB_SEARCH_HOST_BREAKER_REFUSALS=3` |
| Бюджет прогона и учёт стоимости по этапам | `bot/utils/costs.py`, миграция `042_campaign_costs.sql`, хуки в LLM-клиентах, строка «💶 Расход» в отчёте | `CAMPAIGN_BUDGET_USD=5`, `WEB_SEARCH_SCRAPE_API_COST_USD`, `WEB_SEARCH_PAID_QUERY_COST_USD` |
| Префильтр до LLM (сделка, площадь участка) | `bot/analysis_pipeline/prefilter.py`, `store.task_context` | — |

Принятые решения (владелец не ответил, выбраны умолчания):
- «20 порталов клиента» = `SPAIN_PORTALS`;
- allowlist строгий по умолчанию;
- бюджет общий для всех сервисов, через таблицу `campaign_costs`, по умолчанию $5.

Известные ограничения Phase −1:
- breaker хранит счётчики в памяти: после рестарта сайт снова получает N попыток;
- scrape API записывается в журнал на каждый вызов, включая отказы (так бюджет не перерасходуется);
- интервью в Telegram и соцпоиск в бюджет не входят; reduction-агенты пишут расходы без `campaign_id`;
- почему поисковик вообще вернул словари (запросы LLM или бан Google/Bing на IP VPS), без логов не установлено.
  Фильтр теперь отсекает их в любом случае.
- **Миграцию 042 нужно применить на VPS** (`scripts/apply_migrations.sh`) до деплоя кода.

## 2. Что осталось (шаги 4–7 исходной задачи)

- **Шаг 4 выполнен 2026-10-10** — [PROVIDERS.md](PROVIDERS.md): пять акторов Apify, Scrape.do, официальный API и таблица 20 доменов с ценами/оценками часов/первичными ссылками. Владелец выбрал axlymxp + Scrape.do и разрешил фазы 0–1 ответом «yes» (2026-10-10).
- **Шаг 5** — реализация `PLAN.md`, фазы 0–5 (ListingSource, ApifyIdealistaSource, миграция 043, `layer="api"`,
  Scrape.do, отчёты).
- **Шаг 6** — живой smoke-тест с лимитом $1.
- **Шаг 7** — CHECKLIST.md построчно и обновление этого файла.

### Результат шага 4 (2026-10-10)

- Предложен `axlymxp/idealista-scraper` для land/sale/ES: явный `lands`, $1/1k результатов. Выбор **утверждён владельцем** для разработки; требуется земельный JSON и подтверждение plot_m2/географии Мадрида. `dz_omar` — резерв; опубликованный пример только жилья.
- Scrape.do: Hobby $29/250k credits; Idealista минимум 10 credits, с render 25. Цена credits из `Scrape.do-Request-Cost`, query auth `token`, ES подтверждена. Поддержка Fotocasa пока не подтверждена.
- Официальный Idealista API: заявка; публичные лимиты, цена, land и срок выдачи ключа не подтверждены.
- Все способы 20 порталов — рекомендации до VPS проверки. Allowlist не менялся. Деньги не потрачены, внешние API не запускались, настройки и миграции не добавлены.
- Риск будущего smoke: `over_budget()` проверяет прошлые расходы без резервирования и fail-open при ошибке БД; для жёсткого $1 нужны верхние цены/координация сервисов и лимит Apify на стороне провайдера.
- Проверка шага 4: документация и полнота 20 строк; pytest/Postgres/ruff заново не запускались (исполняемый код не изменён). Предыдущие 1417 passed — результат Phase −1, не этого исследования.
- Выбор владельца получен: выполнить фазу 0 DESIGN.md и фазу 1, затем предусмотренная остановка. Платные вызовы/VPS требуют отдельного разрешения согласно промпту ниже.

### Фаза 0 выполнена (2026-10-10)

- [DESIGN.md](DESIGN.md): точки интеграции, SourceListing/ListingSource, JSON-LD и plot_m2, первый раунд/идемпотентность, store/counters, безопасный fallback и gates перед live.
- Выбор провайдера утверждён; Phase 1 ограничена контрактами, Literal и CHECK 043. pages_api и долговечный claim запуска реализуются последующими аддитивными миграциями.
- Проверки в изолированных локальных контейнерах Python 3.11/PostgreSQL 16: полный pytest **1416 passed, 1 skipped** (268.63 s); пропуск — существующий браузерный тест с фиксированным `/opt/pw-browsers/chromium`. После настройки пути этот тест отдельно **1 passed**. Все 1417 тестов выполнены успешно, включая PostgreSQL. Ruff чисто; golden **70 passed**.
- В тестовом контейнере установлены requirements.txt/requirements-dev.txt, Chromium и Linux Docker Compose CLI для проверок compose; рабочие Docker-сервисы и VPS не менялись. Команды полной проверки: `docker exec -e PYTHONPATH=. real-estate-phase1-python python -m pytest -q -o addopts=""`, затем `docker exec real-estate-phase1-python ruff check bot tests`. Три TEST_DATABASE_URL направлены только в отдельную `bot_test`.
- Следующий шаг: foundation фазы 1, затем контрольная остановка перед фазой 2. Платные вызовы не разрешены.

## 3. Как запустить тесты локально

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
# Postgres для postgres-тестов (любой *_test):
export SYSTEM_TEST_DATABASE_URL=postgresql://postgres@/bot_test?host=/var/run/postgresql
export VERIFICATION_TEST_DATABASE_URL=$SYSTEM_TEST_DATABASE_URL ORCHESTRA_TEST_DATABASE_URL=$SYSTEM_TEST_DATABASE_URL
PYTHONPATH=. .venv/bin/python -m pytest -q -o addopts="" && .venv/bin/ruff check bot tests
```

---

## 4. Промпт для Codex

```text
Ты продолжаешь работу в репозитории Telegram-бота REAL-ESTATE-BOT (лидогенерация по недвижимости в Испании, VPS,
Python 3.11, asyncio, asyncpg, pydantic-settings, docker compose). Ветка: claude/optimistic-feynman-y4ayed —
работай только в ней, коммить и пушь туда же. Отвечай по-русски, коротко, со ссылками файл:строка.

СНАЧАЛА ПРОЧИТАЙ ЦЕЛИКОМ:
- docs/idealista-integration/HANDOFF.md (что сделано, решения, ограничения);
- docs/idealista-integration/SCRAPING_ANALYSIS.md (карта пайплайна, сверка ТЗ с кодом, диагноз);
- docs/idealista-integration/{PLAN,TECHNICAL_SPECIFICATION,CHECKLIST,AGENTS}.md;
- .claude/skills/real-estate-pipeline/SKILL.md (инварианты проекта);
- bot/web_search/{worker,store,models,queries,urls,scrape_api,settings}.py, bot/utils/costs.py,
  bot/campaign/{runner,final_report,metrics,summary}.py, bot/services/db/migrations/039-042.

ПРАВИЛА (не нарушать):
- Facebook-часть не трогать (SAFETY_*, окна, discovery).
- Никакого своего обхода DataDome и капч: stealth, патчи браузера, решатели капч запрещены. Разблокировку делает
  только внешний провайдер (Scrape.do) или API (Apify / официальный API Idealista).
- Ничего не устанавливать на VPS и не тратить деньги на внешние API без явного «ок» владельца. Исключение —
  smoke-тест шага 6 с жёстким лимитом $1.
- Секреты только через settings/.env; в .env.example — только плейсхолдеры. Ключи не логировать (repr=False,
  коды ошибок без URL с токеном).
- Новая настройка: поле в settings.py + строка в .env.example с комментарием + строка в docker-compose.yml для
  нужного сервиса (это проверяет tests/test_deployment.py) + строка в docs.
- Миграции только добавлять. Следующая — 043. Внести её в оба списка scripts/apply_migrations.sh
  (это проверяет tests/test_project_foundation.py).
- Если сервис импортирует новый пакет, его Dockerfile должен копировать этот пакет (tests/test_image_imports.py).
- Каждый внешний вызов (Apify, Scrape.do) записывать в журнал bot.utils.costs: стадия "api" для Apify, "scrape"
  для Scrape.do, с реальной ценой. До вызова проверять costs.over_budget(). Вызовы не должны блокировать event loop:
  только async-клиенты или asyncio.to_thread, плюс таймауты.
- Не уверен — проверь по коду или вебу. Не можешь проверить — спроси, не угадывай. Номера строк, имена методов и
  названия акторов из пакета — это предположения.
- После каждой фазы: полный pytest (вместе с Postgres-тестами, см. HANDOFF §3) + ruff, короткое резюме, [x] в
  docs/idealista-integration/PLAN.md, коммит. ⛔ СТОП после шага 4 и после фаз 1, 3 и 5 — показать результат и
  ждать «ок».

ШАГ 4. Проверка провайдеров (веб-поиск, только актуальные данные, со ссылками и датами)
- Apify: акторы для Idealista. Для каждого: поддерживает ли участки (terrenos) и продажу в Мадриде, поля output
  (цена, площадь участка/постройки, сделка, URL, адрес), цена (за результат / за запуск), рейтинг, число
  пользователей, дата последнего обновления. Проверить azzouzana/idealista-scraper и dz_omar/idealista-scraper-api,
  найти альтернативы. Выбрать один и обосновать.
- Scrape.do: тарифы, стоимость запроса обычного / render / super (residential), гео ES, поддержка сайтов с DataDome
  (Idealista, Fotocasa), формат API (token в query или в заголовке) — что поменять в ScrapeApiClient.
- Официальный API Idealista (developers.idealista.com): условия, лимиты, сроки получения ключа, есть ли участки.
- Таблица по 20 доменам urls.SPAIN_PORTALS: способ (напрямую / Scrape.do / Apify / официальный API / исключить),
  стоимость при 1k и 10k страниц в месяц, часы на разработку. Для каждого домена указать, чем подтверждён способ.
- Записать в docs/idealista-integration/PROVIDERS.md. ⛔ СТОП, ждать выбора владельца.

ШАГ 5. Реализация PLAN.md, фазы 0–5 (роли из AGENTS.md: сначала архитектурный дизайн, в конце ревью по CHECKLIST)
- Фаза 0: письменный дизайн (в docs/idealista-integration/DESIGN.md):
  - точки интеграции;
  - протокол ListingSource / SourceListing по ТЗ §1–2;
  - как источник встраивается в первый раунд WebSearchWorker._new_round (worker.py, ветка used_count == 0);
  - как результаты идут через store.begin_fetch/finish_fetch, чтобы дальше работали analysis → tolerance → dedup →
    reporting без изменений;
  - текст поста — строка «JSON-LD: {...}» (structured.facts_block) с price, currency, area_m2, plot_m2, rooms,
    address, property_type, deal: analysis-worker и prefilter её уже понимают.
- Фаза 1: bot/web_search/sources/ (ListingSource, SourceListing) + миграция 043:
  - CHECK web_campaign_urls.layer расширить значением 'api' (сейчас в 039: http, render, scrape, none);
  - Literal-типы PageResult.layer/via в models.py расширить ('api');
  - в MemoryWebStore и PostgresWebStore — тесты на реальном Postgres. ⛔ СТОП.
- Фаза 2: ApifyIdealistaSource:
  - async-вызов актора с таймаутом; ошибки TemporaryApifyError / PermanentApifyError (ТЗ §4) по статусам
    SUCCEEDED / FAILED / TIMED-OUT / ABORTED, 429, 5xx;
  - только первый раунд кампании; бюджет до вызова; фактическая цена запуска — в журнал costs (stage "api");
  - при падении Apify кампания продолжается обычным поиском, а в отчёте видно, что случилось (costs.error("api", code));
  - конфиг: APIFY_TOKEN, APIFY_IDEALISTA_ACTOR, лимит результатов, вкл/выкл; по умолчанию выключено.
- Фаза 3: store — begin_fetch/finish_fetch под layer="api":
  - URL, который раньше упал (failed в web_seen_urls, в т.ч. 403 от Idealista), можно взять повторно через API:
    поправить и enqueue (сейчас помечает такой URL duplicate), и begin_fetch;
  - via/layer "api" не увеличивает счётчики отказов хоста (_count_refusal уже считает только http/render — покрыть
    тестом);
  - метка статуса для scrape API "api" занята: переименовать её или выбрать другую для Idealista. ⛔ СТОП.
- Фаза 4: Scrape.do через существующий ScrapeApiClient (не дублировать клиент):
  - добавить режим передачи ключа параметром token и параметры geoCode=es, render/super из настроек;
  - стоимость вызова — WEB_SEARCH_SCRAPE_API_COST_USD (или точнее, по тарифу из шага 4);
  - breaker и бюджет уже действуют на слой scrape — проверить тестом.
- Фаза 5: отчёты:
  - источник отображается как «Idealista (API)» (summary.site_name / site_lines, final_report, metrics: добавить
    pages_api);
  - расход Apify и Scrape.do виден отдельно в строке «💶 Расход» (STAGE_NAMES в final_report уже содержит "api" и
    "scrape");
  - финальное ревью по CHECKLIST.md: регрессии других порталов, утечки секретов. ⛔ СТОП.

ШАГ 6. Живой smoke-тест (только после «ок» владельца; на VPS)
- Применить миграции 042 и 043. Поставить CAMPAIGN_BUDGET_USD=1 и запустить одну кампанию: «участок, Мадрид,
  покупка, ≥2000 м²».
- Сравнить с прошлым прогоном (≈$15, отправлено 0; 31 из 37 «не удалось проверить»; 54 «не та площадь»;
  14 «аренда»): найдено, отправлено, отклонено по причинам, «отсеяно до ИИ», расход по источникам
  (select stage, kind, item, code, sum(units), sum(cost_usd) from campaign_costs where campaign_id=... group by 1,2,3,4).
- Результат записать в docs/idealista-integration/SMOKE_TEST.md.

ШАГ 7. Финал
- Пройти CHECKLIST.md построчно (✅/❌ + доказательство: тест или file:line) и добавить пункты Phase −1 (allowlist,
  сделка в запросах, парсер площади, breaker, бюджет и учёт, префильтр, явные ошибки ИИ в отчёте).
- Обновить docs/idealista-integration/HANDOFF.md: что сделано, решения и почему, все новые настройки .env, как
  запустить, ограничения, следующие шаги.
```
