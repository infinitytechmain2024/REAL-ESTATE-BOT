# Передача v2 (HANDOFF) — черновик D3

Дата: 2026-10-16. Ветка `v2/docs2` = `main` со всеми смерженными потоками v2 (включая C2 и D1a). Ссылки
`файл:строка` проверены по коду этой ветки. План — `docs/v2/MASTER_PLAN.md`, разбивка — `docs/v2/TASKS.md`, откат —
`docs/v2/ROLLBACK.md`, деплой — `docs/v2/DEPLOY.md`, эксплуатация — `docs/v2/OPS.md`, клиенту —
`docs/v2/CLIENT_GUIDE.md`.

> Черновик: D1 (золотой набор на VPS) и D2 (деплой, включение флагов) ещё не выполнены — разделы с «после D2»
> заполнить по их итогам. Статусы ниже — по `docs/v2/tasks.json` (`updated: 2026-10-15`).

## 1. Что сделано по задачам

Всё новое выключено по умолчанию; флаг и откат каждой части — `ROLLBACK.md` §1.

| Задача | Статус | Что | Где в коде | Флаг |
|---|---|---|---|---|
| A1 | done | Apify-порталы первыми; поисковик только если порталы дали < `APIFY_MIN_LISTINGS`; браузерный соцпоиск выкл | `bot/web_search/worker.py:365-367`, `:379-380`, `:399-412` (`_apify_gate`, итог `apify_enough`); `bot/web_search/settings.py:43-44`; `bot/campaign/runner.py:1556-1560` (`_social_worker`) | `ACQUISITION_MODE` |
| A2 | done | Закрытая группа → одно сообщение «вступите» с кнопкой «Вступил ✅» → чтение браузером | миграция `bot/services/db/migrations/047_campaign_group_join.sql`; `bot/campaign/runner.py:507-520` (`_notify_joins`), `:425-426`; `bot/campaign/runs.py:522-545`; `bot/control_plane/service.py:252-253`, `:287-300`; тексты `bot/campaign/status_text.py:30-45` | `CAMPAIGN_JOIN_NOTICES` |
| A3 | blocked | ⛔ Живая проверка Yaencontre и Idealista-`igolaizola` | **не сделано** (ждёт O2, O3); `docs/apify_portals.md:15-16` — «по схеме выдачи» | — |
| A4 | blocked | ⛔ Первый прогон Apify для групп FB | **не сделано** (ждёт O1, O2, O6); процедура `docs/apify_adapter.md:83-98` | `FEATURE_APIFY_FACEBOOK` |
| A5 | done | Модели по этапам, цены | `docs/v2/MODELS.md`; `bot/utils/costs.py` (`PRICES`); v2-id — закомментированные строки в `.env.example` | `OPENROUTER_*_MODEL` |
| A6 | done | Золотой набор и скрипт оценки | `scripts/eval_campaign.sql`, `scripts/eval_campaign.sh`, `docs/v2/GOLDEN_SET.md` | — (только чтение) |
| B1 | done | Пост FB по ссылке → комментарии через Apify → лиды | `bot/control_plane/intake.py:342` (`post_link`), `:456`, `:469` (спека `mode=investors`, `post_link=True`); `bot/acquisition/apify_facebook_comments.py:93` (`ApifyCommentsSource`); `bot/campaign/post_leads.py:86` (`PostLeadWorker`), `:129-179`; `bot/campaign/runner.py:1648-1671`, `:881-885`; миграция `048_campaign_post_reads.sql` | `APIFY_FACEBOOK_COMMENTS_ENABLED` |
| B2 | done | Комментарии под отправленными объектами — через Apify вместо браузера | `bot/campaign/comment_reader.py:29` (`ApifyCommentReader`); `bot/campaign/leads.py:583-604` (`_step_reader`), `:660-664` (дневная квота); `bot/campaign/runner.py:1610-1618`; миграция `049_campaign_comment_reads_run_id.sql` | `LEADS_PROVIDER` |
| B3 | done | Группы из задачи → `campaign_groups` без браузера; `site:`-запросы соцсетей в reach выкл | `bot/campaign/discovery.py:670-703`; `bot/campaign/reach.py:178-186` (`keep_query`); `bot/campaign/runner.py:1425-1432` | под `ACQUISITION_MODE` (+ `FEATURE_APIFY_FACEBOOK`, `APIFY_FACEBOOK_SCOPE`) |
| B4 | done | Отчёт `both`/`investors`: «🏠 Недвижимость» + «💼 Инвесторы», расход по веткам | `bot/campaign/final_report.py:664-720` (`LeadsSummary`, `cost_split`, `investors_section`), `:806-841` (`build`, ветка `both` — `:833`); `bot/campaign/runner.py:1037-1113` | `CAMPAIGN_INVESTOR_REPORT` |
| B5 | done | Порталы P2: Pisos, Habitaclia, Milanuncios (код на моках) | `bot/web_search/sources/portals.py:402`, `:457`, `:500`; `bot/web_search/settings.py:72-80`, `:209-273`; `docs/apify_portals.md:17-19` | `APIFY_PISOS_ENABLED`, `APIFY_HABITACLIA_ENABLED`, `APIFY_MILANUNCIOS_ENABLED` |
| B6 | cut | Instagram/TikTok через Apify | **отрезано** (линия отсечения) | — |
| C1 | done | C1-lite: интервьюер на Claude API по ключу, запасной — OpenRouter, затем правила | `bot/control_plane/anthropic_interviewer.py:41-60` (промпты по режиму), `:88`; `bot/control_plane/main.py:150-161`; промпты `bot/control_plane/prompts/task-*.md` | `ARCHITECT_PROVIDER` |
| C2 | done | Режим `both` «🏡💼 Недвижимость + инвесторы» в меню Telegram; 4 типа задач (объекты, инвесторы, оба, пост-ссылка) голосом/текстом → карточка → кампания | см. §1.1 | `INTAKE_BOTH_MODE` |
| C3 | done | Healthcheck всем сервисам, watchdog, алерты, дневная сводка | `docker-compose.yml` (`healthcheck:` у каждого сервиса, `watchdog` :812); `bot/ops/watchdog.py:167-263`; `bot/{campaign,analysis_pipeline,agents,ops}/healthcheck.py`; `docs/v2/OPS.md` | `WATCHDOG_ENABLED`, профиль `ops` |
| C4 | done | Бэкап, проверка восстановления, ротация логов, план перезагрузки | `scripts/backup.sh`, `scripts/restore_check.sh`; `x-logging` в `docker-compose.yml`; `docs/v2/OPS.md` «Бэкапы», «Перезагрузка VPS» | — (cron владельца) |
| C5 | cut | Общий LLM-клиент | **отрезано** | — |
| D1a | done | Карточка объекта: «Площадь» (участок отдельно), «Телефон», «Агентство» из данных источника | см. §1.2 | `CARD_EXTRA_FIELDS` |
| D1 | blocked | Золотой набор на VPS, разметка, исправления | **не выполнено**: ждёт A3 и ⛔ O1, O2, O7, O10 (C2 и D1a уже закрыты) | — |
| D2 | blocked | Деплой, включение флагов, проверка алертов | **не выполнено**: ждёт D1 и ⛔ O4, O8, O9 | — |
| D3 | blocked | Этот файл, `ROLLBACK.md`, `CLIENT_GUIDE.md` | `docs/v2/` (черновики; закрыть после D2) | — |

Владелец (`tasks.json`): O11, O12 — done; O5 — cut; O1–O4, O6–O10 — `waiting_owner`.

### 1.1 C2: режим «Недвижимость + инвесторы»

- Режим и подпись: `Mode` с `both` — `bot/campaign/spec.py:29`; `MODE_TITLES["both"] = "🏡💼 Недвижимость + инвесторы"`
  — `spec.py:35-39`. Обязательные поля `both` = поля недвижимости, затем инвесторские без повторного места —
  `spec.py:555-571`; карточка «Проверьте задачу» показывает обе части — `spec.py:626-629`.
- Меню: `mode_menu(..., both=...)` выводит третью кнопку только при флаге — `bot/control_plane/intake.py:296-299`;
  флаг передаётся в `TaskIntake` — `intake.py:367-386`, `bot/control_plane/service.py:144-145`. Пример задачи и поля
  «Изменить» для `both` — `intake.py:89-90`, `:108-109`.
- Флаг выкл: выбор `both` отклоняется (`intake.py:414`), сохранённый черновик `both` считается «режим не выбран»,
  меню показывается заново (`intake.py:398-401`). Чтение флага — `bot/control_plane/settings.py:37-38`, `:95`;
  `.env.example:966-969`; `docker-compose.yml:133` (`telegram`).
- В кампанию режим уходит токеном `mode=both` (`intake.py:185`); Orchestra принимает его независимо от флага
  (`bot/orchestra/parser.py:99`, `:138-141`). Дальше `both` — существующий путь: анализ раскладывает на два vertical
  (`bot/analysis_pipeline/main.py:60`), discovery ищет обе ветки (`bot/campaign/discovery.py:252-254`), отчёт B4
  (`final_report.py:833`).
- Интервьюер: для `both` склеиваются промпты обоих режимов, промпт пост-ссылки подключается по маркеру
  `spec.post_link` (`bot/control_plane/anthropic_interviewer.py:41-60`, `:88`); правила — `bot/control_plane/rules.py:181`,
  `:240-246`, `:303-306` (ответ про тикет инвестора не перетирает бюджет объекта).
- БД: миграция `bot/services/db/migrations/050_task_drafts_mode_both.sql` расширяет check `user_task_drafts.mode`
  (`real_estate`, `investors`, `both`); откат — `scripts/rollback/050_task_drafts_mode_both_down.sql`.
- Тесты (моки): `tests/test_task_types_e2e.py` — 4 типа задач текстом и голосом до кампании в очереди (`:72`),
  пост-ссылка (`:97`), нет кнопки без флага (`:119`), флаг выкл по умолчанию (`:138`), Orchestra берёт `mode=both`
  (`:167`), черновик `both` сбрасывается при выключении флага (`:175`), тикет не трогает бюджет (`:187`).

### 1.2 D1a: площадь, телефон, агентство в карточке объекта

- Источник данных — только первая строка поста `JSON-LD: {...}`, которую пишет сайтовый этап из `SourceListing`
  портала (`bot/web_search/structured.py:105-106`; поля `plot_m2`, `phone`, `advertiser` —
  `bot/web_search/sources/base.py:25-27`). Разбор — `source_line_facts` (`bot/analysis_pipeline/formatters.py:12-31`);
  в payload находки поля попадают только при флаге **и** `evidence.platform == "website"`
  (`formatters.py:34-42`). Из текста поста и от модели телефон не берётся (`bot/analysis_pipeline/cards.py:185-188`).
- Вывод: `cards.py:162-164` (флаг), `:174-182` (`_area`): «Площадь: 180 м², участок 2 500 м²», для участка —
  «2 500 м² (участок)»; `:229-240` — строки «Площадь», «Телефон», «Агентство» только для недвижимости, не для инвесторов.
  `area_m2` модели не подменяется данными источника: участок идёт отдельным полем `plot_m2`.
- Флаг `CARD_EXTRA_FIELDS=false` — `.env.example:579-580`; `docker-compose.yml:350` (`analysis-pipeline`), `:447`
  (`analysis-worker`), `:531` (`campaign-runner`).
- Reduction-режим (`bot/agents/reduction.py:72-74`, `:173-180`) эти поля не несёт: при включённом флаге там возможна
  только строка «Площадь» из `area_m2` модели, без участка, телефона и агентства.
- Тесты (моки): `tests/test_finding_cards.py:242-246`, `tests/test_analysis_pipeline.py:475-491`.

## 2. Архитектура «как сейчас»

### Сервисы (`docker-compose.yml`, проект `real-estate-monitor`, VPS `/opt/real-estate-bot`)

| Сервис | Код | Что делает в v2 |
|---|---|---|
| `telegram` | `bot/control_plane/` | интервью, голос → текст, меню режимов (включая «Недвижимость + инвесторы», C2), карточка «Проверьте задачу», пост-ссылка (B1), кнопка «Вступил ✅» (A2), архитектор на Claude (C1) |
| `campaign-runner` | `bot/campaign/`, `bot/web_search/` | кампании, Apify-порталы (A1, B5), отбор, карточки (D1a), отчёт (B4), лиды из комментариев (B1, B2), reach, уведомления «вступите» (A2) |
| `analysis-worker` | `bot/analysis_pipeline/` | префильтр → LLM → факты с цитатами (`both` раскладывается на два vertical, `main.py:60`); поля карточки D1a |
| `facebook-runner` (+ `facebook-collector`, профиль `collector`) | `bot/facebook_collector/` | группы FB: браузер или Apify (`FEATURE_APIFY_FACEBOOK`) |
| `browser` | `bot/browser_session/` | профиль оператора FB (Playwright) |
| `watchdog` (профиль `ops`) | `bot/ops/` | алерты и дневная сводка владельцу |
| `agent-reach-worker`, `scrapling-worker`, `reduction-worker`, `verification` | `bot/agent_reach/`, `bot/scrapling_connector/`, `bot/agents/`, `bot/verification/` | вспомогательные, в v2 не менялись |
| `postgres`, `redis`, `searxng`, `caddy` | — | инфраструктура |

### Миграции v2

Все добавляющие (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, расширение check); список —
`scripts/apply_migrations.sh` (строки 78-81); откат — `scripts/rollback/04x|050_*_down.sql`, порядок `ROLLBACK.md` §4.

| № | Файл | Что | Задача |
|---|---|---|---|
| 047 | `047_campaign_group_join.sql` | `campaign_groups.join_notice_at`, `join_confirmed_at` | A2 |
| 048 | `048_campaign_post_reads.sql` | таблица `campaign_post_reads` | B1 |
| 049 | `049_campaign_comment_reads_run_id.sql` | `campaign_comment_reads.run_id` (nullable) | B2 |
| 050 | `050_task_drafts_mode_both.sql` | check `user_task_drafts.mode` допускает `both` | C2 |

### Флаги v2 и где они читаются

Полная таблица со значениями отката и сервисами — `ROLLBACK.md` §1–§2. Включение на VPS — `DEPLOY.md` (D2).
Новые с прошлого черновика: `INTAKE_BOTH_MODE` (`telegram`), `CARD_EXTRA_FIELDS` (`analysis-worker`,
`analysis-pipeline`, `campaign-runner`).

### Деньги

- Журнал: таблица `campaign_costs` (миграция 042), API `bot/utils/costs.py` (`record`, `record_unique`, `error`,
  `skip`, `over_budget`). Пишут все сервисы; цена LLM — `usage.cost` OpenRouter, иначе оценка по `PRICES`
  (`bot/utils/costs.py`).
- Платные Apify-запуски: один биллинг на run (`record_unique`): посты по ссылке `post-read:<кампания>:<url>`
  (`bot/campaign/post_leads.py:195-204`), комментарии под объектами `comment-read:<кампания>:<finding>`
  (`bot/campaign/comment_reader.py:79-83`), порталы — `ApifyListingSource` (`bot/web_search/sources/apify.py`).
  Каждый запуск актора идёт с `maxTotalChargeUsd` (`APIFY_*_MAX_CHARGE_USD`, `*_MAX_USD_PER_POST`, `*_MAX_USD_PER_RUN`).
- Потолок кампании: `CAMPAIGN_BUDGET_USD` (`.env.example`, сейчас 5). При достижении: веб-этап стоп (`budget_cap`),
  анализ и ИИ-проверка пропускают кампанию, Apify-чтение не запускается (`post_leads.py:133-136`,
  `comment_reader.py:58-60`). Кампания `both` тратит из одного потолка на обе ветки.
- Где видно: строка «💶 Расход» в отчёте (`bot/campaign/final_report.py:634-661`), «По веткам» для `both`
  (`:702-706`), блок 5 `scripts/eval_campaign.sh`, алерт watchdog «бюджет исчерпан», дневная сводка.

### Алерты

`bot/ops/watchdog.py`: контейнеры `exited/restarting/unhealthy` (`:205-217`, через docker.sock), 3 ошибки Apify
подряд, бюджет исчерпан, 2 кампании подряд без карточек, портал вернул 0 объявлений (`:219-240`), сводка за сутки
(`:242-258`) в `OWNER_ALERT_CHAT_ID`. Что значит каждый алерт и что делать — `docs/v2/OPS.md` «Алерты».

### Отчёты

- Пользователю, один раз по завершении: «📋 Отчёт по поиску» (`bot/campaign/runner.py:1037`,
  `bot/campaign/final_report.py:806`). Пост по ссылке без `CAMPAIGN_INVESTOR_REPORT` — короткий итог
  (`runner.py:1115-1126`). Для режима `both` раздел «💼 Инвесторы» и «По веткам» — только при
  `CAMPAIGN_INVESTOR_REPORT=true`; включать вместе с `INTAKE_BOTH_MODE`.
- Владельцу: технический «Итог поиска» (`runner.py:1011`), дневная сводка watchdog.
- Владельцу/аналитику: `scripts/eval_campaign.sh <campaign-uuid>` (только чтение).

## 3. Что НЕ сделано или перенесено

| Что | Статус | Почему / что нужно |
|---|---|---|
| B6 Instagram/TikTok через Apify | cut | линия отсечения (`TASKS.md` §4) |
| C5 общий LLM-клиент | cut | риск регрессий перед сдачей; Claude API есть там, где важнее — интервью (C1-lite) |
| C1-full (сервис `architect` на Agent SDK) | cut | новая зависимость (O5); выбран C1-lite (O12) |
| A3 и живые сэмплы P2 (B5) | blocked | ждут O3 («ок» на ≤ $0.05 и ≤ $0.02 × 3) и O2; до них Yaencontre, Idealista-`igolaizola`, Pisos, Habitaclia, Milanuncios — по схеме из Apify Store (`docs/apify_portals.md:15-19`) |
| A4 (Apify для групп FB), D1 (золотой набор) | blocked | ждут O1 (compliance §4), O2, O6, O7, O10; D1 ещё ждёт A3 |
| D2 деплой | blocked | ждёт D1, O4, O8, O9 |
| Включение `CARD_EXTRA_FIELDS` для клиента | ⛔ после O1 | телефон частного продавца — персональные данные (§4 п. 7) |

## 4. Известные риски (из ревью и сверки с кодом)

1. **Поля акторов не проверены живым прогоном.** Мапперы групп FB (`bot/facebook_collector/apify/mapper.py`),
   комментариев (`bot/acquisition/apify_facebook_comments.py`) и порталов кроме Fotocasa написаны по документации
   Apify Store. Первый прогон может дать 0 строк — правка маппера 1–2 ч (sources). До A3/A4/B5-live не включать для
   клиента. Это же касается `phone`/`advertiser`/`plot_m2` для D1a: у Pisos и Milanuncios телефона нет вовсе
   (`bot/web_search/sources/portals.py:453`, `:563`).
2. **Окно сбоя БД при запуске актора.** Перед `launch` чтение получает `run_id = pending:<uuid>`
   (`mark_launching`, `post_leads.py`, `leads.py`); после удачного `launch` заглушка заменяется настоящим id. Если
   запись упала, второй запуск не делается: строка с `pending:*` (на следующем тике или после `recover()`)
   закрывается `failed launch_unconfirmed` и списывается по потолку (`code='estimated'`). Явный отказ Apify на
   запуске (429, 4xx) заглушку снимает и идёт обычным путём requeue без оценки. Дневная квота Apify
   (`apify_reads_today`) считает и `pending:*`: такой запуск мог быть оплачен, поэтому квота не занижается.
3. **Две квоты комментариев.** Браузер — `SAFETY_MAX_FACEBOOK_COMMENT_READS_PER_DAY` (считает все чтения со
   `started_at`, `leads.py:656-658`), Apify — `APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY` (только с `run_id`). При
   переключении `LEADS_PROVIDER` в течение дня браузерная квота учитывает и Apify-чтения. Посты по ссылке (B1) под
   дневную квоту не попадают — их ограничивают `APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST` и `CAMPAIGN_BUDGET_USD`.
4. **docker.sock.** `:ro` не делает Docker API read-only: контейнер `watchdog` имеет права уровня хоста
   (`OPS.md` «Безопасность сокета»). Сокет монтируется всегда, когда включён профиль `ops`, даже при пустом
   `WATCHDOG_DOCKER_SOCKET`. Решение владельца — O11 (принято).
5. **B3 зависит от настроек FB-адаптера.** Группы из задачи ставятся в очередь без браузера, но окна запускает
   раннер, ему нужна строка профиля FB в состоянии `ready`, а Apify читает окна только при
   `campaigns.acquisition_provider='apify'` или `APIFY_FACEBOOK_SCOPE=all` (`bot/campaign/discovery.py:690-693`,
   `docs/apify_adapter.md:67`). Иначе группы читает браузер.
6. **Id моделей 5.5 не сверены.** `anthropic/claude-{sonnet,haiku,opus}-5.5` (OpenRouter) и `claude-sonnet-5-5`
   (Claude API, `.env.example:572`) и их цены записаны без проверки (`docs/v2/MODELS.md`). Неверный id → 400 →
   пустая кампания; в D2 — по одному проверочному вызову на этап, при 400 — прежний id (`ROLLBACK.md` §2).
7. **⛔ Телефон в карточке — персональные данные.** При `CARD_EXTRA_FIELDS=true` строка «Телефон» показывает номер
   продавца так, как его дал портал; у частника это ПДн (GDPR). Включать для клиента только после решения O1
   (`COMPLIANCE` §4). До этого флаг выключен; площадь и агентство без телефона отдельным флагом не включаются.
8. **Откат кода при черновиках `both`.** Старый код (до C2) не знает режима `both`: если в `user_task_drafts` есть
   такой черновик, интервью может упасть. Перед откатом кода:
   `update user_task_drafts set mode = null where mode = 'both';` (черновик живёт 24 ч, пользователь просто
   выберет режим заново). Кампании `both` в `campaigns` старый код обрабатывает (путь `/campaign` был и до C2).
   Откат схемы 050 отказывает, пока такие черновики есть (`ROLLBACK.md` §4).
9. **`both` без отчёта инвесторов.** При `INTAKE_BOTH_MODE=true` и `CAMPAIGN_INVESTOR_REPORT=false` пользователь
   получает отчёт только по недвижимости. Включать два флага вместе (D2).
10. **Кулдаун алертов в памяти** — после рестарта watchdog возможен один повтор (`OPS.md` «Watchdog»).
11. **Лиды одного поста видны всем кампаниям с этой ссылкой.** `stored_people_of_campaign` (`bot/campaign/runs.py`)
    не фильтрует по `campaign_id`: два пользователя с одной ссылкой увидят одних и тех же комментаторов. Это
    публичные комментарии, но стоит знать (security-ревью v2).
12. **`repr=False` есть не везде.** `OPENROUTER_API_KEY` в `bot/campaign/settings.py:62` скрыт, но в ряде settings
    секреты без `repr=False`: `bot/web_search/settings.py:88`, `bot/agents/settings.py:16`,
    `bot/analysis_pipeline/settings.py:8` (ключи OpenRouter), `telegram_token`/`database_url` в
    `bot/campaign/settings.py:18-19` и `bot/control_plane/settings.py:18-19`, `BROWSER_SESSION_API_TOKEN`
    (`bot/campaign/settings.py:22`, `bot/agent_reach/settings.py:13`, `bot/facebook_collector/settings.py:20`,
    `bot/browser_session/settings.py:28`), `REDIS_URL` (`bot/browser_session/settings.py:15`). Объекты settings целиком
    не логируются, но при добавлении такого лога секреты утекут.
13. **docker.sock (к п. 4).** При желании заменить прямой сокет на docker-socket-proxy, разрешающий один
    `GET /containers`; тогда у watchdog не будет прав уровня хоста.
14. **Ссылки на посты от пользователя.** Через `asking="sources.required"` (`rules.py:402-403`) пользователь сам может
    добавить до 12 ссылок на посты; каждая это платное чтение в пределах `max_usd_per_post` и `over_budget`.

15. **`cap_drop: [ALL]` у watchdog не проверен на реальном docker.sock.** В `docker-compose.yml` (`watchdog`) стоят
    `cap_drop: [ALL]`, `read_only: true`, `no-new-privileges:true`, сокет `:ro` и `group_add` с `WATCHDOG_DOCKER_GID`.
    Тесты проверяют только текст compose-файла. Не проверено живьём: хватает ли доступа к сокету процессу без
    capabilities (доступ идёт по группе сокета, а не по `CAP_DAC_OVERRIDE`; если сокет не `root:docker 660`, а процесс
    не root, чтение даст «permission denied»), и совпадает ли реальный GID с `WATCHDOG_DOCKER_GID` (умолчание 999).
    Признак беды: в логе `watchdog` ошибка доступа к сокету или алерт «docker недоступен». Проверка при D2:
    `DEPLOY.md` (ж), тест `stop analysis-worker`. Если не работает: сначала сверить GID (`stat -c %g /var/run/docker.sock`),
    потом убрать `cap_drop` отдельным коммитом с «ок» владельца, либо оставить `WATCHDOG_DOCKER_SOCKET` пустым
    (алерты БД и сводка работают без сокета).
16. **`COMPOSE_PROFILES=ops` не проверен на VPS.** Предполагается, что `docker compose --env-file .env` берёт
    `COMPOSE_PROFILES` из `.env`, поэтому `scripts/update.sh` (`up -d --force-recreate`) и `docker compose up -d`
    поднимают `watchdog` без `--profile ops`. Это поведение Compose v2, но живьём на VPS оно не проверялось и версия не сверялась
    (`docker compose version`). Если watchdog после `update.sh` не появился в `ps`, используйте
    `docker compose --env-file .env --profile ops up -d watchdog`. Профиль также включает остальные сервисы под
    `COMPOSE_PROFILES`, если значение содержит их имена (`collector`, `analysis` и др.): при дописывании `ops`
    сохраняйте уже заданные профили в той же строке.
17. **`facebook-runner` не выходил в интернет (исправлено 17.10).** Сервис был только в `backend` (`internal: true`),
    поэтому на проде при `FEATURE_APIFY_FACEBOOK=true` запуск актора давал `apify_launch_unknown` ×2 и оценку $0.50 ×2
    (`ConnectError: Temporary failure in name resolution`), запусков в Apify не было. Теперь `facebook-runner` и
    `facebook-collector` в `[backend, egress]` (`docker-compose.yml`); `tests/test_deployment.py` требует `egress` у
    каждого сервиса с `APIFY_TOKEN`. Нужен `up -d` с новым compose. Оценки $0.50 в `campaign_costs` по тем кампаниям
    остаются как есть. Там же `.env` содержал заглушку `APIFY_FACEBOOK_ACTOR_BUILD=ВСТАВЬТЕ_НОМЕР_БИЛДА`, и она ушла в
    `apify_runs.actor_build`: теперь билд обязан быть вида `0.1.23` или тегом (`beta`), не `latest`; иначе сервис падает
    на старте (то же для `APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD`).

## 5. Как читать золотой набор и оценку

- `docs/v2/GOLDEN_SET.md` — таблица G1–G9: «Точных/отправлено» (ручная разметка карточек), «Мусорных хостов»,
  «Лидов», «$», дата и id кампании. Цели: ≥ 70 % точных и 0 мусорных хостов (DoD-1), ≥ 60 % разумных лидов
  (DoD-2), `$` ниже `CAMPAIGN_BUDGET_USD`. Пустая ячейка = ещё не снято. Для DoD-3 в D1 прогнать задачу `both`
  через меню (нужен `INTAKE_BOTH_MODE=true` на время прогона).
- `./scripts/eval_campaign.sh <campaign-uuid>` на VPS — только чтение. Блоки: 1 воронка (сколько карточек
  отправлено), 3 хосты (мусор считается вручную по `example_url`), 4 порталы Apify (`state`/`error_code`),
  5 `campaign_costs` по stage/item, 6 лиды, 7 какие модели платили. Порядок переноса чисел в таблицу —
  `GOLDEN_SET.md` «Как снять базовую линию». Вывод хранить вне репозитория (там контакты).
- Диагностика «почему плохо» — `.claude/skills/diagnose-campaign/SKILL.md`; стоимость моделей — `docs/v2/MODELS.md`.

## 6. Доска задач

`docs/v2/board.html` (открыть в браузере), источник — `docs/v2/tasks.json`, пересборка
`python3 scripts/build_v2_board.py`. Правило статусов — `MASTER_PLAN.md` §4 «Зависимости и автозапуск».
