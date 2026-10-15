# Передача v2 (HANDOFF) — черновик D3

Дата: 2026-10-15. Ветка `v2/ops` = `main` со всеми смерженными потоками v2. Ссылки `файл:строка` проверены по коду
этой ветки. План — `docs/v2/MASTER_PLAN.md`, разбивка — `docs/v2/TASKS.md`, откат — `docs/v2/ROLLBACK.md`, деплой —
`docs/v2/DEPLOY.md`, эксплуатация — `docs/v2/OPS.md`, клиенту — `docs/v2/CLIENT_GUIDE.md`.

> Черновик: D1 (золотой набор на VPS) и D2 (деплой, включение флагов) ещё не выполнены — разделы с «после D2»
> заполнить по их итогам. `docs/v2/tasks.json` и `docs/v2/board.html` в этой ветке отстают от кода (статусы
> `review/blocked` у смерженных задач) — обновить при закрытии D3.

## 1. Что сделано по задачам

Всё новое выключено по умолчанию; флаг и откат каждой части — `ROLLBACK.md` §1.

| Задача | Что | Где в коде | Флаг |
|---|---|---|---|
| A1 | Apify-порталы первыми; поисковик только если порталы дали < `APIFY_MIN_LISTINGS`; браузерный соцпоиск выкл | `bot/web_search/worker.py:365-367`, `:379-380`, `:399-412` (`_apify_gate`, итог `apify_enough`); `bot/web_search/settings.py:43-44`; `bot/campaign/runner.py:1556-1560` (`_social_worker`) | `ACQUISITION_MODE` |
| A2 | Закрытая группа → одно сообщение «вступите» с кнопкой «Вступил ✅» → чтение браузером | миграция `bot/services/db/migrations/047_campaign_group_join.sql`; `bot/campaign/runner.py:507-520` (`_notify_joins`), `:425-426`; `bot/campaign/runs.py:522-545`; `bot/control_plane/service.py:252-253`, `:287-300`; тексты `bot/campaign/status_text.py:30-45` | `CAMPAIGN_JOIN_NOTICES` |
| A3 | ⛔ Живая проверка Yaencontre и Idealista-`igolaizola` | **не сделано** (ждёт O2, O3); `docs/apify_portals.md:15-16` — «по схеме выдачи» | — |
| A4 | ⛔ Первый прогон Apify для групп FB | **не сделано** (ждёт O1, O2, O6); процедура `docs/apify_adapter.md:83-98` | `FEATURE_APIFY_FACEBOOK` |
| A5 | Модели по этапам, цены | `docs/v2/MODELS.md`; `bot/utils/costs.py:43-59` (`PRICES`); v2-id — закомментированные строки в `.env.example` | `OPENROUTER_*_MODEL` |
| A6 | Золотой набор и скрипт оценки | `scripts/eval_campaign.sql`, `scripts/eval_campaign.sh`, `docs/v2/GOLDEN_SET.md` | — (только чтение) |
| B1 | Пост FB по ссылке → комментарии через Apify → лиды | `bot/control_plane/intake.py:338-345` (`post_link`), `:439-456` (`_post_task`); `bot/acquisition/apify_facebook_comments.py:93` (`ApifyCommentsSource`); `bot/campaign/post_leads.py:86` (`PostLeadWorker`), `:129-179`; `bot/campaign/runner.py:1648-1671`, `:881-885`; миграция `048_campaign_post_reads.sql` | `APIFY_FACEBOOK_COMMENTS_ENABLED` |
| B2 | Комментарии под отправленными объектами — через Apify вместо браузера | `bot/campaign/comment_reader.py:29` (`ApifyCommentReader`); `bot/campaign/leads.py:583-604` (`_step_reader`), `:660-664` (дневная квота); `bot/campaign/runner.py:1610-1618`; миграция `049_campaign_comment_reads_run_id.sql` | `LEADS_PROVIDER` |
| B3 | Группы из задачи → `campaign_groups` без браузера; `site:`-запросы соцсетей в reach выкл | `bot/campaign/discovery.py:670-703`; `bot/campaign/reach.py:178-186` (`keep_query`); `bot/campaign/runner.py:1425-1432` | под `ACQUISITION_MODE` (+ `FEATURE_APIFY_FACEBOOK`, `APIFY_FACEBOOK_SCOPE`) |
| B4 | Отчёт `both`/`investors`: «🏠 Недвижимость» + «💼 Инвесторы», расход по веткам | `bot/campaign/final_report.py:664-720` (`LeadsSummary`, `cost_split`, `investors_section`), `:806-841` (`build`); `bot/campaign/runner.py:1037-1113` | `CAMPAIGN_INVESTOR_REPORT` |
| B5 | Порталы P2: Pisos, Habitaclia, Milanuncios (код на моках) | `bot/web_search/sources/portals.py:402`, `:457`, `:500`; `bot/web_search/settings.py:72-80`, `:209-273`; `docs/apify_portals.md:17-19` | `APIFY_PISOS_ENABLED`, `APIFY_HABITACLIA_ENABLED`, `APIFY_MILANUNCIOS_ENABLED` |
| B6 | Instagram/TikTok через Apify | **отрезано** (линия отсечения) | — |
| C1 | C1-lite: интервьюер на Claude API по ключу, запасной — OpenRouter, затем правила | `bot/control_plane/anthropic_interviewer.py:59`; `bot/control_plane/main.py:150-161`; промпты `bot/control_plane/prompts/task-*.md` | `ARCHITECT_PROVIDER` |
| C2 | Режим `both` в Telegram, 4 типа задач | **в коде не найден** — см. §3 | — |
| C3 | Healthcheck всем сервисам, watchdog, алерты, дневная сводка | `docker-compose.yml` (`healthcheck:` у каждого сервиса, `watchdog` :807-849); `bot/ops/watchdog.py:167-263`; `bot/{campaign,analysis_pipeline,agents,ops}/healthcheck.py`; `docs/v2/OPS.md` | `WATCHDOG_ENABLED`, профиль `ops` |
| C4 | Бэкап, проверка восстановления, ротация логов, план перезагрузки | `scripts/backup.sh`, `scripts/restore_check.sh`; `x-logging` в `docker-compose.yml`; `docs/v2/OPS.md` «Бэкапы», «Перезагрузка VPS» | — (cron владельца) |
| C5 | Общий LLM-клиент | **отрезано** | — |
| D1–D2 | Золотой набор на VPS, деплой, включение флагов | **ещё не выполнены** (⛔ O1, O2, O4, O7–O10) | — |
| D3 | Этот файл, `ROLLBACK.md`, `CLIENT_GUIDE.md` | `docs/v2/` | — |

## 2. Архитектура «как сейчас»

### Сервисы (`docker-compose.yml`, проект `real-estate-monitor`, VPS `/opt/real-estate-bot`)

| Сервис | Код | Что делает в v2 |
|---|---|---|
| `telegram` | `bot/control_plane/` | интервью, голос → текст, карточка «Проверьте задачу», пост-ссылка (B1), кнопка «Вступил ✅» (A2), архитектор на Claude (C1) |
| `campaign-runner` | `bot/campaign/`, `bot/web_search/` | кампании, Apify-порталы (A1, B5), отбор, карточки, отчёт (B4), лиды из комментариев (B1, B2), reach, уведомления «вступите» (A2) |
| `analysis-worker` | `bot/analysis_pipeline/` | префильтр → LLM → факты с цитатами (`both` раскладывается на два vertical, `main.py:60`) |
| `facebook-runner` (+ `facebook-collector`, профиль `collector`) | `bot/facebook_collector/` | группы FB: браузер или Apify (`FEATURE_APIFY_FACEBOOK`) |
| `browser` | `bot/browser_session/` | профиль оператора FB (Playwright) |
| `watchdog` (профиль `ops`) | `bot/ops/` | алерты и дневная сводка владельцу |
| `agent-reach-worker`, `scrapling-worker`, `reduction-worker`, `verification` | `bot/agent_reach/`, `bot/scrapling_connector/`, `bot/agents/`, `bot/verification/` | вспомогательные, в v2 не менялись |
| `postgres`, `redis`, `searxng`, `caddy` | — | инфраструктура |

### Флаги v2 и где они читаются

Полная таблица со значениями отката и сервисами — `ROLLBACK.md` §1–§2. Включение на VPS — `DEPLOY.md` (D2).

### Деньги

- Журнал: таблица `campaign_costs` (миграция 042), API `bot/utils/costs.py` (`record`, `record_unique`, `error`,
  `skip`, `over_budget`). Пишут все сервисы; цена LLM — `usage.cost` OpenRouter, иначе оценка по `PRICES`
  (`costs.py:43-59`).
- Платные Apify-запуски: один биллинг на run (`record_unique`): посты по ссылке `post-read:<кампания>:<url>`
  (`bot/campaign/post_leads.py:195-204`), комментарии под объектами `comment-read:<кампания>:<finding>`
  (`bot/campaign/comment_reader.py:79-83`), порталы — `ApifyListingSource` (`bot/web_search/sources/apify.py`).
  Каждый запуск актора идёт с `maxTotalChargeUsd` (`APIFY_*_MAX_CHARGE_USD`, `*_MAX_USD_PER_POST`, `*_MAX_USD_PER_RUN`).
- Потолок кампании: `CAMPAIGN_BUDGET_USD` (`.env.example:318`, сейчас 5). При достижении: веб-этап стоп
  (`budget_cap`), анализ и ИИ-проверка пропускают кампанию, Apify-чтение не запускается
  (`post_leads.py:133-136`, `comment_reader.py:58-60`).
- Где видно: строка «💶 Расход» в отчёте (`bot/campaign/final_report.py:634-661`), «По веткам» для `both`
  (`:702-706`), блок 5 `scripts/eval_campaign.sh`, алерт watchdog «бюджет исчерпан», дневная сводка.

### Алерты

`bot/ops/watchdog.py`: контейнеры `exited/restarting/unhealthy` (`:205-217`, через docker.sock), 3 ошибки Apify
подряд, бюджет исчерпан, 2 кампании подряд без карточек, портал вернул 0 объявлений (`:219-240`), сводка за сутки
(`:242-258`) в `OWNER_ALERT_CHAT_ID`. Что значит каждый алерт и что делать — `docs/v2/OPS.md` «Алерты».

### Отчёты

- Пользователю, один раз по завершении: «📋 Отчёт по поиску» (`bot/campaign/runner.py:1037`,
  `bot/campaign/final_report.py:806`). Пост по ссылке без `CAMPAIGN_INVESTOR_REPORT` — короткий итог
  (`runner.py:1115-1126`).
- Владельцу: технический «Итог поиска» (`runner.py:1011`), дневная сводка watchdog.
- Владельцу/аналитику: `scripts/eval_campaign.sh <campaign-uuid>` (только чтение).

## 3. Что НЕ сделано или перенесено

| Что | Статус | Почему / что нужно |
|---|---|---|
| **C2: режим `both` в меню Telegram** | в коде нет: `Mode` без `both` (`bot/campaign/spec.py:29`), `MODE_TITLES` два режима (`spec.py:35-38`), `EXAMPLES` без `both` (`bot/control_plane/intake.py:89`) | **Расхождение с планом, блокирует DoD-3 через меню.** `both` сейчас возникает только из текста `/campaign <цель>`, где есть и недвижимость, и инвесторы (`bot/campaign/architect.py:342-347`); B4 готов его отчитать. Архитектору: проверить, не потерялся ли PR C2 при мерже, иначе вернуть C2 в `ready` (builder, 4 ч, флаг `INTAKE_BOTH_MODE`) |
| B6 Instagram/TikTok через Apify | отрезано | линия отсечения (`TASKS.md` §4) |
| C5 общий LLM-клиент | отрезано | риск регрессий перед сдачей; Claude API есть там, где важнее — интервью (C1-lite) |
| C1-full (сервис `architect` на Agent SDK) | отрезано | новая зависимость (O5); выбран C1-lite (O12) |
| A3 и живые сэмплы P2 (B5) | не сделаны | ждут O3 («ок» на ≤ $0.05 и ≤ $0.02 × 3) и O2; до них Yaencontre, Idealista-`igolaizola`, Pisos, Habitaclia, Milanuncios — по схеме из Apify Store (`docs/apify_portals.md:15-19`) |
| A4 (Apify для групп FB), D1 (золотой набор) | не сделаны | ждут O1 (compliance §4), O2, O6, O7, O10 |
| D2 деплой | не сделан | ждёт D1, O4, O8, O9 |

## 4. Известные риски (из ревью и сверки с кодом)

1. **Поля акторов не проверены живым прогоном.** Мапперы групп FB (`bot/facebook_collector/apify/mapper.py`),
   комментариев (`bot/acquisition/apify_facebook_comments.py`) и порталов кроме Fotocasa написаны по документации
   Apify Store. Первый прогон может дать 0 строк — правка маппера 1–2 ч (sources). До A3/A4/B5-live не включать для
   клиента.
2. **Окно сбоя БД при запуске актора.** Между `launch` и сохранением `run_id` (`post_leads.py:137-153`,
   `comment_reader.py:61-72`) run уже оплачен в Apify. Если запись в БД упала, второй запуск не делается, чтение
   закрывается `run_id_not_saved` и списывается по потолку (`code='estimated'`). Следствие для B2: дневная квота
   Apify считает только строки с `run_id` (`leads.py:660-664`), такой запуск в неё не попадёт — квота может быть
   превышена на число таких сбоев.
3. **Две квоты комментариев.** Браузер — `SAFETY_MAX_FACEBOOK_COMMENT_READS_PER_DAY` (считает все чтения со
   `started_at`, `leads.py:656-658`), Apify — `APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY` (только с `run_id`). При
   переключении `LEADS_PROVIDER` в течение дня браузерная квота учитывает и Apify-чтения. Посты по ссылке (B1) под
   дневную квоту не попадают — их ограничивают `APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST` и `CAMPAIGN_BUDGET_USD`.
4. **docker.sock.** `:ro` не делает Docker API read-only: контейнер `watchdog` имеет права уровня хоста
   (`OPS.md` «Безопасность сокета»). Сокет монтируется всегда, когда включён профиль `ops`
   (`docker-compose.yml:838`), даже при пустом `WATCHDOG_DOCKER_SOCKET`. Решение владельца — O11.
5. **B3 зависит от настроек FB-адаптера.** Группы из задачи ставятся в очередь без браузера, но окна запускает
   раннер, ему нужна строка профиля FB в состоянии `ready`, а Apify читает окна только при
   `campaigns.acquisition_provider='apify'` или `APIFY_FACEBOOK_SCOPE=all` (`bot/campaign/discovery.py:690-693`,
   `docs/apify_adapter.md:67`). Иначе группы читает браузер.
6. **Id моделей 5.5 не сверены.** `anthropic/claude-{sonnet,haiku,opus}-5.5` (OpenRouter) и `claude-sonnet-5-5`
   (Claude API, `.env.example:572`) и их цены записаны без проверки (`MODELS.md:7-12`). Неверный id → 400 →
   пустая кампания; в D2 — по одному проверочному вызову на этап, при 400 — прежний id (`ROLLBACK.md` §2).
7. **Карточка объекта без отдельных строк «Площадь» и «Телефон».** `render_card` выводит сделку, тип, цену,
   локацию, район, комнаты, этаж, особенности, уверенность, «Кратко», ссылку (`bot/analysis_pipeline/cards.py:186-241`);
   площадь и телефон попадают в карточку только через «Особенности»/«Кратко». Мастер-план (§1) обещает их явно —
   проверить на D1 и, если нужно, мини-задача analyst.
8. **Кулдаун алертов в памяти** — после рестарта watchdog возможен один повтор (`OPS.md` «Watchdog»).

## 5. Как читать золотой набор и оценку

- `docs/v2/GOLDEN_SET.md` — таблица G1–G9: «Точных/отправлено» (ручная разметка карточек), «Мусорных хостов»,
  «Лидов», «$», дата и id кампании. Цели: ≥ 70 % точных и 0 мусорных хостов (DoD-1), ≥ 60 % разумных лидов
  (DoD-2), `$` ниже `CAMPAIGN_BUDGET_USD`. Пустая ячейка = ещё не снято.
- `./scripts/eval_campaign.sh <campaign-uuid>` на VPS — только чтение. Блоки: 1 воронка (сколько карточек
  отправлено), 3 хосты (мусор считается вручную по `example_url`), 4 порталы Apify (`state`/`error_code`),
  5 `campaign_costs` по stage/item, 6 лиды, 7 какие модели платили. Порядок переноса чисел в таблицу —
  `GOLDEN_SET.md` «Как снять базовую линию». Вывод хранить вне репозитория (там контакты).
- Диагностика «почему плохо» — `.claude/skills/diagnose-campaign/SKILL.md`; стоимость моделей — `docs/v2/MODELS.md`.

## 6. Доска задач

`docs/v2/board.html` (открыть в браузере), источник — `docs/v2/tasks.json`, пересборка
`python3 scripts/build_v2_board.py`. Правило статусов — `MASTER_PLAN.md` §4 «Зависимости и автозапуск».
