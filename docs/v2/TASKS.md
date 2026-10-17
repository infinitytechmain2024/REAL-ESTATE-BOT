# REAL-ESTATE-BOT v2 — разбивка задач (A1–D3)

Дата: 2026-10-15 (пятница). База: `main` @ `d59620e` + коммиты v2-доков (`b2bca6f`). Мастер-план:
`docs/v2/MASTER_PLAN.md`. Правила: `CLAUDE.md`. Источник правды по статусам: `docs/v2/tasks.json`.

Все ссылки `файл:строка` проверены по коду этой ветки, не по памяти. Всё новое — за флагом, выключено по умолчанию.
Тесты — только моки (`httpx.MockTransport`, фейковые store), ни одного live-запроса в CI.

---

## 0. Что выяснилось при сверке с кодом (отличия от мастер-плана)

| # | Мастер-план | В коде | Следствие |
|---|---|---|---|
| 1 | «Мусор на входе» (D1, D7, D8, D9 из SCRAPING_ANALYSIS) | Уже починено: `WEB_SEARCH_DOMAIN_POLICY=strict` (`web_search/settings.py:98`, `worker.py:669`), сделка в каждом запросе, префильтр до LLM, журнал `campaign_costs` и бюджет (`tests/test_scraping_fixes.py`) | A1 сужается до «Apify первым, поисковик — запасной, соцпоиск выкл» |
| 2 | «Соцпоиск `site:tiktok.com`» в `campaign/runner.py` | Два разных механизма: браузерный `bot/social_search` (`SOCIAL_SEARCH_PLATFORMS=instagram,tiktok,linkedin`, `runner.py:1419`) и `site:`-шаблоны инвесторского reach через SearXNG (`campaign/reach.py:57-65`) | A1 выключает браузерный соцпоиск; `site:`-шаблоны reach режет B3 |
| 3 | «analysis-worker молча теряет пост» (D3) | Уже починено: `analysis_pipeline/main.py:47-80` (halt на 401/402, error с кодом) | ничего |
| 4 | Режимы intake | `Mode = Literal["real_estate","investors"]` (`campaign/spec.py:29`); `both` есть только в `CampaignPlan.vertical` (`campaign/models.py:24`) | «both» в Telegram надо добавить (C2) |
| 5 | Лиды из комментариев: `campaign_comment_reads` | PK `(campaign_id, finding_id)`, `finding_id not null` (миграция 026) — пост по ссылке без finding туда не ляжет | B1 — новая таблица `campaign_post_reads` (048) |
| 6 | Нотификации оператору из facebook-runner | У facebook-runner нет Telegram (compose, сервис `facebook-runner`); messenger есть у campaign-runner (`runner.py:164`) и у telegram (`service.py:147 _tell_owners`) | A2 шлёт уведомление из campaign-runner, кнопка — в telegram |
| 7 | Архитектор на Claude Agent SDK (C1) | Уже есть протокол `Interviewer` (`control_plane/interviewer.py:61`) с реализацией `OpenRouterInterviewer`; карточка «Проверьте задачу» — `intake.py:_card` | Рекомендую **C1-lite**: `AnthropicInterviewer` по тому же протоколу напрямую в Claude API (httpx, без новой зависимости, без нового сервиса). Полный Agent SDK — C1-full, за линией отсечения |
| 8 | Healthcheck «нет у analysis-worker, campaign-runner, reduction-worker, searxng» | Healthcheck есть только у postgres, redis, caddy; у **10** сервисов нет (`docker-compose.yml`); `searxng` без `restart` | C3 шире, чем в плане |
| 9 | Цены моделей | `bot/utils/costs.py:24-32` `PRICES` по префиксу (`anthropic/claude-sonnet` = 3/15) — новые id 5.5 попадут в префикс, но цены могут отличаться | A5 обновляет `PRICES` |
| 10 | Миграции | Последняя — `046_facebook_apify_runs.sql`; следующая **047** | A2 → 047, B1 → 048 (см. §3 про порядок мержа) |
| 11 | Локальная среда | В этом контейнере Python 3.13 и нет pytest; проект на 3.11 | Перед шагом 2: `.venv` на 3.11 (uv/pyenv) или контейнер — проверить в начале исполнения |

---

## 1. Задачи

Формат: цель → DoD; файлы/функции; настройки/миграции; тесты; приёмка; риски; исполнитель, зависимости, часы, ветка; ⛔.

### Поток `v2/acquisition` (пятница)

#### A1 — Режим сбора `ACQUISITION_MODE=apify|legacy`
**Цель.** Apify-порталы идут первыми; SearXNG — только если порталы дали меньше `APIFY_MIN_LISTINGS`; браузерный
соцпоиск выключен. → DoD-1 (0 мусорных сайтов), DoD-5.

**Файлы и функции.**
- `bot/web_search/settings.py:27 WebSearchSettings` — поля `acquisition_mode`, `apify_min_listings`; пробросить в
  `config()` (`settings.py:159`) и `WebSearchConfig` (`worker.py:116`).
- `bot/web_search/worker.py:318 _advance` — перед `_search`/`_new_round` в режиме `apify`: пока есть `source_runs`
  в состоянии `starting/running/ready` — ждать (уже есть `_resume_sources`, `worker.py:429`); когда все
  `completed/failed`: если `sum(len(run.listings))` по `completed` ≥ `apify_min_listings` → `_done(campaign,
  "apify_enough")` без генерации запросов; иначе — обычный `_new_round` (fallback на SearXNG). При `legacy` —
  поведение прежнее, строка в строку.
- `bot/web_search/worker.py:363 _new_round` — в режиме `apify` первый раунд не ставит запросы, пока источники не
  закончили (`_from_sources` остаётся как есть).
- `bot/campaign/settings.py` — `acquisition_mode` (та же переменная); `bot/campaign/runner.py:1419 _social_worker` —
  при `apify` возвращает `(None, None)` (браузерный соцпоиск выключен), лог `campaign.runner.social_disabled_apify`.
- Статусная строка (`worker.py:1096 _line`): «порталы через Apify…» вместо «составляю запросы» на этапе ожидания.

**Настройки** (все три места: settings, `docker-compose.yml` сервис `campaign-runner`, `.env.example`):
`ACQUISITION_MODE` str `legacy` (значения `legacy|apify`); `APIFY_MIN_LISTINGS` int `5` (1–200).
Миграций нет.

**Тесты** (`tests/test_listing_source_worker.py`, `tests/test_web_search.py` — расширять, не дублировать):
1. `apify`, два источника дали суммарно ≥ N → `searcher.search` не вызван, кампания `apify_enough`.
2. `apify`, источники дали < N → запросы сгенерированы, SearXNG вызван (fallback).
3. `apify`, источник `failed` → fallback на поисковик.
4. `legacy` → прежнее поведение (существующие тесты зелёные без правок).
5. `_social_worker` при `apify` → `(None, None)`; при `legacy` — как раньше.
6. `tests/test_deployment.py::test_compose_passes_every_setting_to_its_service` зелёный.

**Приёмка.** `pytest tests/test_listing_source_worker.py tests/test_web_search.py tests/test_deployment.py -q`;
`grep ACQUISITION_MODE docker-compose.yml .env.example`. На VPS (после D2): `select state, detail from
web_campaign_urls …` — нет хостов вне `SPAIN_PORTALS` при `apify_enough`.

**Риски.** `_advance` — центральная машина состояний web-стадии: ошибка ветвления остановит кампанию на
«ожидании»; обязательно `time_cap` остаётся первым. При `apify` без включённых порталов (`sources()` пусто) режим
должен сразу падать в fallback, а не висеть.

Исполнитель: **builder** (+ sources консультирует). Зависимости: нет. **4 ч.** Ветка `v2/acquisition`.

#### A2 — Закрытая группа FB → «вступите» → чтение браузером
**Цель.** Закрытая группа не теряется молча: пользователь получает одно сообщение «Группа X закрыта, вступите:
ссылка», после «Вступил» группа читается браузером. → DoD-4 (ничего не падает молча), без обхода входа.

**Как сейчас.** Apify-провайдер помечает группу `INACCESSIBLE` с кодом `group_private`
(`facebook_collector/apify/provider.py:195-207`), пишет `acquisition_batch_items.last_error_code` и
`monitoring_sources.configuration` (`apify/store.py:133-153`) и один раз ставит браузерный батч
(`queue_browser_fallback`, `store.py:166`; `choose_provider` → `local`, `facebook_collector/main.py:22-29`).
Если аккаунт не состоит в группе, браузер тоже вернёт `INACCESSIBLE` — и всё.

**Файлы и функции.**
- Миграция **047** `047_campaign_group_join.sql` (db): `campaign_groups add column if not exists join_notice_at
  timestamptz, join_confirmed_at timestamptz`; откат `scripts/rollback/047_campaign_group_join_down.sql`;
  регистрация в обоих списках `scripts/apply_migrations.sh`.
- `bot/campaign/runs.py` (store кампании, обновление `campaign_groups` по итогам батча — `runs.py:368-377`):
  новые методы `groups_needing_join(campaign_id)` (activity `INACCESSIBLE`, причина `group_private`,
  `join_notice_at is null`), `mark_join_notice(group_id)`, `confirm_join(group_id)` (ставит
  `join_confirmed_at`, `state='queued'`, activity `UNKNOWN`).
- `bot/campaign/runner.py:460 _follow_window` / `:485 _next_window` — после закрытия окна: для каждой группы из
  `groups_needing_join` → `messenger.send_buttons(chat_id, текст, [("Вступил ✅", f"group:joined:{id}")])`, затем
  `mark_join_notice` (одно уведомление на группу и кампанию). Повторное чтение подтверждённой группы — батч с
  `requested_by='apify_fallback:join:<group_id>'`, чтобы `choose_provider` выбрал браузер.
- `bot/control_plane/service.py:236 handle_callback` — новый префикс `group:joined:<id>` → `confirm_join`
  (через store control-plane; доступ — владелец/оператор), ответ «Проверю группу через браузер».
- Текст уведомления — в `bot/campaign/status_text.py` рядом с остальными строками.

**Настройки.** `CAMPAIGN_JOIN_NOTICES` bool `false` (campaign-runner, telegram; compose + `.env.example`).
При `false` ничего не меняется.

**Тесты.** `tests/test_campaign_runner.py` (фейковый store/messenger): (1) группа `group_private` → ровно одно
сообщение с кнопкой, повторный тик — 0 сообщений; (2) флаг выкл → 0 сообщений; (3) `confirm_join` → группа снова
`queued`, батч с `requested_by='apify_fallback:join:…'` → `choose_provider == "local"`
(`tests/test_facebook_provider.py`); (4) Postgres-тест миграции 047 (`tests/test_campaign_postgres.py`, skip без
`SYSTEM_TEST_DATABASE_URL`); (5) callback `group:joined` в `tests/test_telegram_control_plane.py`.

**Приёмка.** `pytest tests/test_campaign_runner.py tests/test_facebook_provider.py tests/test_telegram_control_plane.py -q`;
`select filename from schema_migrations where filename like '047%'`.

**Риски.** Триггер переходов состояний (миграция 003) — батч подтверждённой группы должен проходить те же
переходы; окна FB ограничены `SAFETY_*` — повторное чтение считается в квоту. Кнопка живёт в telegram, а
отправитель — campaign-runner: формат callback согласовать (`service.py:236` документирует префиксы).

Исполнитель: **builder** + **db** (миграция). Зависимости: нет. **5 ч** (A2a уведомление 3 ч, A2b кнопка и
перечитывание 2 ч — A2b можно перенести). Ветка `v2/acquisition`.

#### A3 — ⛔ Живая проверка Yaencontre и Idealista-`igolaizola`
**Цель.** Мапперы `ApifyYaencontreSource._normalize` (`sources/portals.py:224`) и
`ApifyIdealistaPortalSource._normalize` (`portals.py:294`) написаны по схеме Apify Store, а не по нашей выдаче
(`docs/apify_portals.md`). Проверить на реальных строках, поправить поля. → DoD-1 (площадь участка не теряется).

**Файлы.** `bot/web_search/sources/portals.py` (мапперы, `plot_area` `:43`), фикстуры в
`tests/test_apify_portals.py` (строки из живой выдачи, телефоны/агентства заменены).

**Процедура.** Через коннектор Apify: `fetch-actor-details` (бесплатно) → `call-actor` с `maxItems ≤ 5`,
`maxTotalChargeUsd ≤ 0.02` на актор, задача G1 (участок, Мадрид, продажа). Итого ≤ $0.05. Выдачу → фикстура.
Записать в `docs/apify_portals.md` «проверено живым запуском <дата>».

**Тесты.** Существующие + по одной строке «из живой выдачи» на портал; «нет числа → None»; участок vs дом.

**Приёмка.** `pytest tests/test_apify_portals.py -q`; в `docs/apify_portals.md` колонка «Проверено» заполнена.

**Риски.** Если актор отдаёт площадь только в описании — `plot_area` по тексту, как у Fotocasa; если нет телефона
— поле `None`, не угадывать.

Исполнитель: **sources**. Зависимости: O2 (токен и лимит в Console), O3 («ок» на ≤ $0.05). **2 ч.**
Ветка `v2/acquisition`. ⛔ деньги + live.

#### A4 — ⛔ Включить Apify для FB: 1 открытая группа, ≤ 50 постов
**Цель.** Первый реальный прогон адаптера (`docs/apify_adapter.md`, раздел «Первый ручной прогон»). → DoD-1/2.

**Что делается.** Только `.env` на VPS и SQL — кода нет: `FEATURE_APIFY_FACEBOOK=true`,
`APIFY_FACEBOOK_ACTOR_BUILD=0.99.1538`, `APIFY_FACEBOOK_MAX_POSTS_PER_RUN=50`, `APIFY_FACEBOOK_MAX_USD_PER_RUN=0.5`,
`CAMPAIGN_BUDGET_USD=1`, `update campaigns set acquisition_provider='apify' where id=…`. Deployer готовит блок
команд и проверок (`apify_runs.items_imported > 0`, `campaign_costs` = Apify Billing), sources сверяет поля
маппера `facebook_collector/apify/mapper.py` с живыми строками и правит фикстуры `tests/test_apify_facebook.py`.

**Приёмка.** SQL из `docs/apify_adapter.md` «Проверить»; стоимость сходится с Apify Console.

**Риски.** Формат выдачи актора ≠ документации → 0 импортированных; тогда правка маппера (1–2 ч, sources).

Исполнитель: **sources** + **deployer**; запускает владелец. Зависимости: **O1** (compliance §4), O2, O6 (группа).
**1 ч** (+2 ч если маппер). ⛔ compliance + деньги + live.

#### A5 — Модели по этапам (только `.env`) + `docs/v2/MODELS.md`
**Цель.** Сильные модели на ключевых шагах, дешёвые — на массовых. → DoD-1, DoD-5.

**Файлы.** `.env.example` (строки 142–494: `OPENROUTER_*_MODEL`), `docs/v2/MODELS.md` (новый),
`bot/utils/costs.py:24 PRICES` (цены новых id, чтобы `llm_cost` считал верно).

Таблица этап → переменная → было → станет (id сверить на openrouter.ai перед записью):

| Этап | Переменная | Сейчас | v2 |
|---|---|---|---|
| анализ страницы | `OPENROUTER_ANALYSIS_MODEL` (`analysis_pipeline/settings.py:10`) | sonnet-4.5 | `anthropic/claude-sonnet-5.5` |
| отбор (reviewer) | `OPENROUTER_REVIEW_MODEL` (`campaign/settings.py:87`) | sonnet-4.5 | sonnet-5.5 |
| запросы web | `OPENROUTER_WEB_QUERY_MODEL` (`web_search/settings.py:73`) | gpt-4o-mini | `anthropic/claude-haiku-5.5` |
| план поиска | `OPENROUTER_PLAN_MODEL` | sonnet-4.5 | sonnet-5.5 |
| интервью | `OPENROUTER_INTERVIEW_MODEL` (`control_plane/settings.py:31`) | sonnet-4.5 | sonnet-5.5 (до C1) |
| лиды / reach | `OPENROUTER_LEADS_MODEL`, `OPENROUTER_REACH_MODEL` | gpt-4o-mini | haiku-5.5 |
| отчёт | `OPENROUTER_FINAL_MODEL` | sonnet-4.5 | `anthropic/claude-opus-5.5` |
| match (legacy judge) | `OPENROUTER_MATCH_MODEL` | gpt-4o-mini | без изменений |

Умолчания в коде **не меняются** (правило 2): только `.env.example` + VPS `.env` (D2).

**Тесты.** `tests/test_costs.py`: цена для `anthropic/claude-sonnet-5.5` и `haiku-5.5` ≠ `DEFAULT_PRICE`
по ошибке; `.env.example` парсится (`tests/test_deployment.py`).

**Приёмка.** `MODELS.md` с ценой за 1M и оценкой «$ за кампанию» по формуле из SCRAPING_ANALYSIS §3.5.

**Риски.** Неверный id модели → OpenRouter 400 → `analysis` закрывает посты с `error` (учтено в коде, но кампания
пустая). Поэтому D2 включает один проверочный вызов на каждом этапе.

Исполнитель: **analyst**. Зависимости: нет. **1.5 ч.** Ветка `v2/acquisition`.

#### A6 — Золотой набор и скрипт оценки
**Цель.** Измеримая DoD-1/2: доля точных карточек, мусор, стоимость — до и после. → DoD-1, DoD-2, DoD-5.

**Файлы.** `scripts/eval_campaign.sql` (воронка: ссылки → прочитано → находки → карточки; `why`/`hold_reason`;
хосты; `web_listing_source_runs`; `campaign_costs` по stage/item; лиды `investor_leads`, `campaign_lead_deliveries`),
`docs/v2/GOLDEN_SET.md` (таблица G1–G9 с колонками «ожидание», «точных/отправлено», «мусор», «$», «дата»),
`scripts/eval_campaign.sh` (обёртка `docker compose exec postgres psql … -v cid=`). Основа —
`.claude/skills/diagnose-campaign/SKILL.md` и SQL §5 SCRAPING_ANALYSIS.

**Тесты.** `tests/test_project_foundation.py`: SQL-файл парсится (pglast если есть, иначе skip).

**Приёмка.** `./scripts/eval_campaign.sh <cid>` на VPS выводит таблицы; базовая линия по последней кампании
записана в `GOLDEN_SET.md` (делает владелец с командами от analyst — ⛔ нет, только чтение).

Исполнитель: **analyst**. Зависимости: нет (заполнение G2–G9 — O7). **2 ч.** Ветка `v2/acquisition`.

### Поток `v2/investors` (суббота)

#### B1 — Пост по ссылке → комментарии (Apify) → лиды
**Цель.** Пользователь шлёт URL поста FB → бот читает комментарии через Apify, классифицирует комментаторов и
присылает карточки лидов с профилем и цитатой. → **DoD-2**.

**Переиспользуется.** `ApifyHttp` (`acquisition/apify_client.py:67`: `launch`, `run`, `dataset_page`),
`Comment`/`PostContext`/`Verdict`/`rule_verdict`/`OpenRouterLeadJudge`/`person_card` (`campaign/leads.py:110-420`),
таблицы `investor_leads`, `campaign_lead_deliveries` (026), отправка людей `runner._people` (`runner.py:818`),
`costs.record_unique` для одного биллинга на run.

**Файлы и функции.**
1. Intake (`bot/control_plane/intake.py:383 on_text`): текст = URL поста FB (`leads.is_facebook_post`) →
   черновик `mode="investors"`, `task="лиды из комментариев под постом"`, `spec.sources.required=[url]`,
   сразу карточка «Проверьте задачу» (`_card`) без интервью; `_launch` как обычно → Orchestra →
   `plan_campaign(vertical="investors")` (`orchestra/dispatcher.py:194`). Проверить, что `spec` доезжает в
   `campaigns.spec` (миграция 035).
2. Новый `bot/acquisition/apify_facebook_comments.py`: `ApifyCommentsSource(http, actor_id, build, max_items,
   max_charge_usd)` — `launch` актора `apify/facebook-comments-scraper` (`startUrls=[url]`, `resultsLimit`,
   `includeNestedComments=true`, `viewOption`), ожидание как в `ApifyFacebookProvider._await`
   (`apify/provider.py:241`), чтение датасета страницами, маппер → `list[Comment]` (автор, `profile_url`,
   `profile_key`, текст, `comment_url`); текст поста — из `apify/facebook-posts-scraper` (1 элемент) или из поля
   комментария, если актор отдаёт; ничего не угадывать.
3. Миграция **048** `048_campaign_post_reads.sql` (db): `campaign_post_reads(campaign_id uuid fk, post_url text,
   state queued|reading|done|failed, provider text, run_id text, comments_found int, leads_found int, cost_usd
   numeric, error_code text, created_at, read_at; pk (campaign_id, post_url))`. Откат
   `scripts/rollback/048_…_down.sql`. Store — по образцу `apify/store.py` (`set_config('app.actor')`).
4. Новый `bot/campaign/post_leads.py`: `PostLeadWorker` (цикл в campaign-runner рядом с `_comment_worker`,
   `runner.py:1469`): на открытие кампании с FB-URL в `spec.sources.required` → строка в `campaign_post_reads`;
   шаг: claim → `ApifyCommentsSource.read(url)` → `judge` (`OpenRouterLeadJudge`, fallback `rule_verdict`) →
   `Lead` → `PostgresLeadStore.save_leads` (`leads.py:593`, `campaign_id` = кампания, `location` = null) →
   `finish`. Стоимость: `costs.record_unique("api", key=f"post-read:{cid}:{url_key}", …)`; `campaign_costs`.
5. `runner._people` (`runner.py:818-852`): для кампании с `campaign_post_reads` — люди по `campaign_id`
   (новый `runs.stored_people_of_campaign`), не по городу; карточка `person_card` + цитата (уже есть).
6. Отчёт: `_final_report` сейчас пропускает `investors` (`runner.py:986`) — для пост-ссылки короткий итог
   «Комментариев: N, лидов: M, $» (в B4 объединяется с разделом инвесторов).

**Настройки** (campaign-runner, telegram для intake; compose + `.env.example`): `APIFY_FACEBOOK_COMMENTS_ENABLED`
bool `false`; `APIFY_FACEBOOK_COMMENTS_ACTOR` `apify/facebook-comments-scraper`;
`APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD` str `""` (обязателен при включении, как у групп);
`APIFY_FACEBOOK_POSTS_ACTOR` `apify/facebook-posts-scraper`; `APIFY_FACEBOOK_COMMENTS_MAX_PER_POST` int `200`;
`APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST` float `1.0`; `APIFY_FACEBOOK_COMMENTS_TIMEOUT_SECONDS` int `300`.
Токен — общий `APIFY_TOKEN`. Roles лидов остаются `investor|buyer` (CHECK в 026); «агент/шум» — не лид, не
сохраняется, но считается в `campaign_costs` как `skip`.

**Тесты** (`tests/test_post_leads.py`, `httpx.MockTransport` на `api.apify.com`): (1) 30 комментариев → 5 лидов,
карточки с профилем и цитатой, один `campaign_costs` на run с `maxTotalChargeUsd` в теле запуска;
(2) 0 комментариев → `done`, 0 лидов, отчёт «лидов нет»; (3) закрытый пост (актор `FAILED`/пусто с ошибкой) →
`failed` с `error_code`, пользователю «пост недоступен»; (4) повторный тик не запускает актор второй раз
(`run_id` сохранён); (5) intake: URL → карточка `investors` с источником (`tests/test_user_intake.py`);
(6) Postgres-тест 048; (7) нет токена/ключа в логах.

**Приёмка.** `pytest tests/test_post_leads.py tests/test_user_intake.py tests/test_comment_leads.py -q`;
на VPS после D2 (⛔ O1, O2): один пост из O7, `select * from campaign_post_reads; select count(*) from investor_leads
where campaign_id=…`, стоимость ≈ `$0.0025 × комментарии` в `campaign_costs`.

**Риски.** Поля выдачи актора комментариев известны по документации — живой прогон (D1) может потребовать правки
маппера; GDPR: хранить только публичное (имя, ссылка, цитата) — уже так в `investor_leads`; `COMPLIANCE.md` §4 —
дополнить абзацем про комментарии (analyst, 15 мин).

Исполнитель: **builder** (intake, worker) + **sources** (актор, маппер) + **db** (048) + **analyst** (промпт
`OpenRouterLeadJudge` под 4 класса). Зависимости: нет (код на моках); **live — только после O1, O2**.
**9 ч.** Ветка `v2/investors`. ⛔ compliance перед live.

#### B2 — Лиды под отправленными объектами через Apify
**Цель.** `CommentLeadWorker` (`leads.py:485`) читает комментарии не браузером, а `ApifyCommentsSource` из B1.
→ DoD-2, разгрузка браузерного профиля.

**Файлы.** `bot/campaign/leads.py:485 CommentLeadWorker` — параметр `reader` (протокол `CommentReader.read(url) ->
(post_text, comments)`), ветка без `claim_profile`/`acquire` при провайдере apify; `runner.py:1469 _comment_worker`
— выбор по `LEADS_PROVIDER`; `campaign/settings.py:129-138`.

**Настройки.** `LEADS_PROVIDER` str `browser` (`browser|apify`). При `apify` и выключенном
`APIFY_FACEBOOK_COMMENTS_ENABLED` — предупреждение и `browser`.

**Тесты** (`tests/test_comment_leads.py`): при `apify` `LeadBrowser.acquire` не вызывается; лиды сохранены; при
`browser` — существующие тесты без правок.

**Приёмка.** `pytest tests/test_comment_leads.py -q`.

**Риски.** Квота `SAFETY_MAX_FACEBOOK_COMMENT_READS_PER_DAY` относится к браузеру — для Apify считать отдельно
(`APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY` int `40`).

Исполнитель: **builder**. Зависимости: B1. **2 ч.** Ветка `v2/investors`.

#### B3 — Группы инвесторов FB через Apify → лиды
**Цель.** Инвесторская кампания читает заданные владельцем открытые группы через Apify, посты инвесторов идут в
анализ (vertical `investors`, уже поддержан в `analysis_pipeline/main.py:59`) и в карточки. → DoD-2.

**Как сейчас.** Группы находит только браузерный discovery по seed-запросам (`campaign/discovery.py:390
plan_seeds`); список групп из задачи никуда не попадает. Adapter Apify читает любую кампанию с
`acquisition_provider='apify'` или `APIFY_FACEBOOK_SCOPE=all`.

**Файлы.** `bot/campaign/discovery.py` — `_seed_required_groups`: URL `facebook.com/groups/…` из
`spec.sources.required` добавляются в `campaign_groups` (`state='queued'`, activity `UNKNOWN`) до браузерного
поиска (проверить API `PostgresDiscoveryStore`); при `ACQUISITION_MODE=apify` и наличии таких групп браузерный
discovery пропускается. `bot/campaign/reach.py:57 TEMPLATES` — при `apify` шаблоны `site:instagram.com`,
`site:x.com`, `site:reddit.com` пропускаются (LinkedIn-сниппеты и `web` остаются — мастер-план §3). Intake:
«Группы: ссылки через запятую» как поле `sources.required` (вопрос уже есть в `rules.py`? проверить, иначе
добавить в `FIELDS`).

**Настройки.** Нет новых (используются `ACQUISITION_MODE`, `FEATURE_APIFY_FACEBOOK`, `APIFY_FACEBOOK_SCOPE`).

**Тесты.** `tests/test_campaign_discovery.py`: группы из spec → `campaign_groups` без браузера;
`tests/test_investor_reach.py`: при `apify` нет `site:instagram.com`-запросов; e2e на памяти: пост из группы с
`vertical=investors` → finding → карточка (`tests/test_campaign_runner.py`).

**Приёмка.** `pytest tests/test_campaign_discovery.py tests/test_investor_reach.py -q`; на VPS (после A4):
`select name, activity, state from campaign_groups where campaign_id=…`.

**Риски.** Закрытые группы инвесторов → A2 (уведомление «вступите»). Discovery — сложный модуль с
верификацией; правка должна быть до его первого шага, не внутри.

Исполнитель: **sources** (+ builder для intake-поля). Зависимости: B1 (общий классификатор), **A4 для live**.
**3 ч.** Ветка `v2/investors`.

#### B4 — `both`: две ветки параллельно, отчёт раздельно, бюджет виден по веткам
**Цель.** → **DoD-3**.

**Как сейчас.** Стадии и так независимые циклы (`runner.py:1330-1338`); `analysis` раскладывает `both` на два
vertical (`main.py:59`); `_people` работает для `investors|both` (`runner.py:821`); `_final_report` пропускает
только `investors` (`runner.py:986`), но раздела про инвесторов в отчёте нет; `campaign_costs` без vertical.

**Файлы.** `bot/campaign/final_report.py:742 FinalReporter.build` — новый аргумент `leads: LeadsSummary`
(лидов сохранено/отправлено, reach-контактов, постов прочитано); текст: «🏠 Недвижимость» (как сейчас) +
«💼 Инвесторы» (для `investors|both`), расходы: общая сумма + строки `stage/item` с пометкой ветки по `item`
(`apify:facebook-comments…` → инвесторы, `listing-source:*` → недвижимость; `llm` — общий). `runner._final_report`
— убрать пропуск `investors`, собирать `LeadsSummary` из `runs.people_sent` (`runs.py:823`) и нового
`post_reads_summary` (B1). `bot/utils/costs.py` — **без миграции**: `Entry.item` уже различает источники;
раздельный потолок `CAMPAIGN_BUDGET_USD_INVESTORS` — только если останется время (тогда миграция 049
`campaign_costs.vertical`).

**Настройки.** Нет обязательных. Опционально `CAMPAIGN_BUDGET_USD_INVESTORS` float `0` (0 = общий бюджет).

**Тесты.** `tests/test_final_report.py`: `both` → оба раздела; `investors` → только инвесторский; `real_estate` —
без изменений (снапшоты текста существующих тестов не трогать). `tests/test_campaign_runner.py`: `both` —
за один `step` работают web-стадия, `_queue_comments`, `_people`.

**Приёмка.** `pytest tests/test_final_report.py tests/test_campaign_runner.py -q`; на VPS кампания G9 → один
отчёт с двумя разделами и расходом.

**Риски.** `final_report.py` — 777 строк форматирования с тестами-снапшотами; менять добавлением, не переписыванием.

Исполнитель: **analyst** (текст отчёта) + **builder** (runner). Зависимости: B1 (для `post_reads_summary`;
можно начать с заглушкой). **5 ч.** Ветка `v2/investors`.

### Поток `v2/portals` (суббота)

#### B5 — Порталы P2: Pisos, Habitaclia, Milanuncios
**Цель.** Шесть порталов через Apify вместо своего загрузчика. → DoD-1 (покрытие, 0 мусора).

**Файлы.** `bot/web_search/sources/portals.py` — три подкласса `_PortalSource` по образцу `ApifyFotocasaSource`
(`portals.py:110-172`: `kinds`, `deals`, `_plan`, `_normalize`, `plot_area`), `bot/web_search/settings.py:192
sources()` — три блока, `docker-compose.yml` (campaign-runner), `.env.example`, `docs/apify_portals.md`.
Акторы: `logiover/pisos-com-property-scraper`, `trev0n/habitaclia-com-spain-scraper`, `rastriq/milanuncios-scraper`
— перед кодом `fetch-actor-details` (вход/выход/цена, бесплатно), записать в PR.

**Настройки** (×3, по шаблону скилла `add-apify-source` §4): `APIFY_PISOS_ENABLED` `false`, `APIFY_PISOS_ACTOR`,
`APIFY_PISOS_MAX_RESULTS` `20`, `APIFY_PISOS_MAX_CHARGE_USD` `0.10`; то же `HABITACLIA`, `MILANUNCIOS`.

**Тесты** (`tests/test_apify_portals.py`): на каждый портал — `supports`, тело запуска (`maxTotalChargeUsd`),
маппинг строки, чужая сделка/муниципалитет/страница поиска → отброшено, «нет числа → None», `sources()` строит
источник только при флаге.

**Приёмка.** `pytest tests/test_apify_portals.py tests/test_deployment.py -q`; затем ⛔ живой сэмпл ≤ $0.02 на актор
(O3), фикстуры из живой выдачи, строка в `docs/apify_portals.md`.

**Риски.** Сторонние акторы (не `igolaizola`) могут быть заброшены или менять схему — закреплённый билд,
алерт «0 объявлений у портала» (C3). Milanuncios смешивает частников и мусор — фильтр `classify_url`.

Исполнитель: **sources**. Зависимости: O3 (для живого сэмпла; код — сразу). **6 ч** (2 на портал; при нехватке
времени Milanuncios переносится). Ветка `v2/portals`. ⛔ live-сэмплы.

#### B6 — (опционально) Instagram / TikTok через Apify
`bot/acquisition/apify_social.py` на `ApifyHttp`, `apify/instagram-scraper` и `clockworks/tiktok-scraper` по
хэштегам/словам → `AcquiredPost.collected_post_row()` → анализ. Флаги `APIFY_INSTAGRAM_ENABLED`,
`APIFY_TIKTOK_ENABLED` (`false`), потолки. Тесты на моках. **6 ч**, sources, deps B1. **За линией отсечения.**

### Поток `v2/architect` (воскресенье)

#### C1 — Архитектор задачи на Claude (рекомендация: C1-lite)
**Цель.** Разбор задачи сильной моделью по API-ключу (не подписке), с fallback. → поток пользователя §1 мастер-плана.

**C1-lite (рекомендую).** Новый `bot/control_plane/anthropic_interviewer.py`: `AnthropicInterviewer` реализует
протокол `Interviewer` (`interviewer.py:61-66`) — тот же промпт/схема `InterviewTurn` (`parse_turn`,
`interviewer.py:193`), но `POST https://api.anthropic.com/v1/messages` (httpx, заголовки `x-api-key`,
`anthropic-version`), строгий JSON, таймаут 90 с, `costs.llm`. Тексты «скиллов» задачи (`task-real-estate`,
`task-investors`, `task-post-link`) — системный промпт в `bot/control_plane/prompts/` (markdown в образе
telegram). `control_plane/main.py:139-141` — выбор по `ARCHITECT_PROVIDER`; при ошибке/таймауте —
`OpenRouterInterviewer`, затем `RuleInterviewer` (уже есть `_turn`, `intake.py:540-554`). Никакой новой зависимости,
никакого нового сервиса. **5 ч**, builder + analyst (промпт). Зависимости: O4 (ключ — для live; код на моках).

**C1-full (за линией).** Сервис `architect` на `claude-agent-sdk` (`bot/architect/`,
`docker/architect.Dockerfile`, compose, HTTP `POST /plan`), telegram ходит в него как в browser-сервис.
**8 ч**, ⛔ O5 (зависимость) + O4.

**Настройки.** `ARCHITECT_PROVIDER` str `openrouter` (`openrouter|anthropic`); `ANTHROPIC_API_KEY` str `""`
(repr=False, не логируется); `ANTHROPIC_INTERVIEW_MODEL` str `claude-sonnet-5-5` (id сверить);
`ANTHROPIC_TIMEOUT_SECONDS` int `90`. Сервис telegram; compose + `.env.example` (пустой ключ).

**Тесты** (`tests/test_interviewer.py` расширить): валидный ответ → `InterviewTurn`; вне схемы → fallback на
OpenRouter-мок; 401/таймаут → fallback, пользователь получает тот же вопрос; стоимость записана; ключа нет в
логах/`repr`.

**Приёмка.** `pytest tests/test_interviewer.py tests/test_intake_dialogue.py -q`; `grep -r ANTHROPIC_API_KEY
docker-compose.yml .env.example`.

**Риски.** Другой формат ответа Anthropic (tool_use vs text JSON) — использовать `tool_use` с JSON-схемой турна,
это надёжнее `json_object`. Подписка Claude в проде запрещена — только ключ.

Ветка `v2/architect`.

#### C2 — Проводка: голос → архитектор → «Проверьте задачу» → кампания (4 типа)
**Цель.** Четыре типа задач от голоса до запуска: недвижимость, инвесторы, both, пост-ссылка. → DoD-3, поток §1.

**Как сейчас.** Голос: `service.py:316 handle_voice` → `stt.py` → `intake.on_text` (`transcript` показывается
«Я услышал»). Режимы intake: только `real_estate|investors` (`spec.py:29 Mode`, `MODE_TITLES spec.py:35`,
`mode_menu intake.py:284`). Orchestra принимает `mode=both` (`parser.py:122`, `plan_campaign` с `Vertical`).

**Файлы.** `bot/campaign/spec.py:29` `Mode` + `"both"`, `MODE_TITLES` «Недвижимость + инвесторы»,
`missing_hard` (`spec.py:552`): для `both` — объединение требований обоих режимов; `summary_ru` (`:609`);
`bot/control_plane/rules.py:231 next_path` и `:300` — ветка `both`; `intake.py:79 EXAMPLES`, `mode_menu`,
`_enrich` (`:194`); `interviewer.py` промпт — описание режима `both`. Пост-ссылка — из B1. E2E-тест на памяти:
`MemoryIntakeStore` + фейковый sink → 4 команды Orchestra с правильными `mode=`/`spec`.

**Настройки.** Нет (режим `both` — выбор пользователя в меню; скрыть кнопку можно флагом `INTAKE_BOTH_MODE`
bool `false` — да, за флагом, по правилу 2).

**Тесты.** `tests/test_user_intake.py`, `tests/test_intake_dialogue.py`, `tests/test_voice_commands.py`:
4 сценария до `launch`, `parse_campaign_goal` → `vertical`; флаг выкл → меню без «both».

**Приёмка.** `pytest tests/test_user_intake.py tests/test_intake_dialogue.py tests/test_voice_commands.py
tests/test_orchestra_dispatcher.py -q`.

**Риски.** `TaskSpec.mode` — `Literal` в pydantic-модели, которая хранится в `campaigns.spec` и `task_drafts`;
добавление значения обратно совместимо, но CHECK-ограничений в БД на mode быть не должно (проверить 035, 018).

Исполнитель: **builder**. Зависимости: B1, B4 (C1 — не блокирует: при его отсутствии работает OpenRouter
интервьюер). **4 ч.** Ветка `v2/architect`.

#### C5 — (опционально) Общий LLM-клиент `bot/llm/client.py`
`LLM_PROVIDER=openrouter|anthropic`, перевод 11 мест (`OpenRouterAnalyzer`, `OpenRouterQueryGenerator`,
`OpenRouterSearchPlanner`, `OpenRouterInterviewer`, `OpenRouterUnderstanding`, `OpenRouterLeadJudge`,
`OpenRouterReachJudge`, `OpenRouterRecommender`, reviewer `relevance.py`, social `queries.py`, агенты `agents/llm.py`).
**8 ч**, builder + analyst, deps O4. **За линией отсечения** (высокий риск регрессий перед сдачей; C1-lite даёт
Claude API там, где он важнее всего).

### Поток `v2/ops` (воскресенье)

#### C3 — 24/7: healthcheck всем, watchdog, алерты, дневная сводка
**Цель.** → **DoD-4**.

**Как сейчас.** Healthcheck только у `postgres`, `redis`, `caddy`; `restart: unless-stopped` у всех, кроме
`searxng` (`docker-compose.yml:608-616`). Модули `healthcheck.py` есть у control_plane, facebook_collector,
agent_reach, browser_session, scrapling_connector, verification (импорт-проверка); нет у campaign-runner,
analysis-worker, reduction-worker.

**Файлы.**
1. `docker-compose.yml` — `healthcheck` у 10 сервисов: python-сервисы `["CMD","python","-m",
   "bot.<pkg>.healthcheck"]` (`interval: 60s`, `timeout: 20s`, `retries: 3`, `start_period: 60s`), новые
   `bot/campaign/healthcheck.py`, `bot/analysis_pipeline/healthcheck.py`, `bot/agents/healthcheck.py` (импорт +
   `select 1` к БД с таймаутом 5 с); `searxng` — `restart: unless-stopped` + `wget -qO- localhost:8080/healthz`;
   `browser` — `curl localhost:8090/healthz` (есть `browser_session/healthcheck.py`).
2. Новый сервис `watchdog` (`bot/ops/watchdog.py`, образ campaign-runner, `profiles: ["ops"]` или флаг):
   - состояние контейнеров — Docker Engine API через `/var/run/docker.sock:ro` и `httpx.AsyncHTTPTransport(uds=…)`
     (без новой зависимости); ⛔ **вопрос владельцу**: сокет даёт контейнеру права на хост — альтернатива
     без сокета: таблица `service_heartbeats` (каждый сервис пишет `now()` раз в минуту; +1 ч и правки в 6 сервисах);
   - из БД: 3 ошибки Apify подряд (`campaign_costs kind='error' stage='api'`), бюджет исчерпан (`sum(cost_usd)
     ≥ CAMPAIGN_BUDGET_USD` по кампании), 0 карточек в 2 завершённых кампаниях подряд (`campaign_findings`),
     портал дал 0 объявлений (`web_listing_source_runs state='completed' and jsonb_array_length(listings)=0`);
   - алерт в Telegram (`sendMessage` через httpx, `TELEGRAM_TOKEN`) в `OWNER_ALERT_CHAT_ID`, не чаще одного раза
     в `WATCHDOG_ALERT_COOLDOWN_MINUTES` на ключ; дневная сводка в `WATCHDOG_DAILY_SUMMARY_HOUR` (кампании,
     карточки, лиды, $, ошибки).
3. `docs/v2/OPS.md` раздел «Алерты: что значит и что делать».

**Настройки** (сервис `watchdog`; compose + `.env.example`): `WATCHDOG_ENABLED` bool `false`;
`OWNER_ALERT_CHAT_ID` int `0`; `WATCHDOG_POLL_SECONDS` int `60`; `WATCHDOG_ALERT_COOLDOWN_MINUTES` int `30`;
`WATCHDOG_DAILY_SUMMARY_HOUR` int `9`; `WATCHDOG_DOCKER_SOCKET` str `""` (пусто = контейнеры не проверяются).
Миграций нет (кулдаун в памяти; после рестарта возможен один повтор алерта — допустимо).

**Тесты** (`tests/test_watchdog.py`, `MockTransport` для Docker API и Telegram; `MemoryLedger`-подобный фейк
store): контейнер `unhealthy`/`exited` → один алерт, повтор в кулдауне → 0; 3 ошибки Apify → алерт; бюджет →
алерт; 0 результатов ×2 → алерт; сводка один раз в день; токен не в логах; `tests/test_deployment.py` (healthcheck
у каждого сервиса — добавить проверку `every service has healthcheck and restart`).

**Приёмка.** `pytest tests/test_watchdog.py tests/test_deployment.py -q`; `docker compose config | grep -c
healthcheck` = число сервисов; на VPS: `docker compose kill analysis-worker` → алерт ≤ 2 мин (D2).

**Риски.** Healthcheck с обращением к БД при её рестарте пометит все сервисы unhealthy → шторм алертов: алерт
«N сервисов разом» одной строкой. `start_period` обязателен, иначе рестарт-петля при медленном старте браузера.

Исполнитель: **deployer** (compose, healthcheck, OPS.md) + **builder** (`watchdog.py`, тесты). Зависимости: нет
(O8 — chat id — нужен к D2). **6 ч.** Ветка `v2/ops`. ⛔ решение про docker.sock.

#### C4 — Бэкап по cron, ротация логов, перезагрузка VPS
**Файлы.** `scripts/backup.sh` (`pg_dump | gzip` с `set -o pipefail`, проверка размера > 10 КБ, ротация 14 дней,
`/root/backups`), `scripts/restore_check.sh` (восстановление в `postgres_check` контейнер на копии, `select
count(*) from campaigns`), `docker-compose.yml` — `x-logging: &logging {driver: json-file, options: {max-size:
"20m", max-file: "5"}}` всем сервисам, `docs/v2/OPS.md` (cron `0 3 * * *`, чистка `apify_raw_items`, план
перезагрузки: бэкап → `docker compose down` → `reboot` → `up -d` → проверки).

**Тесты.** `tests/test_deployment.py`: скрипт содержит `pipefail` и не содержит `drop`/`rm -rf` вне `/root/backups`;
у каждого сервиса `logging`.

**Приёмка.** На VPS (D2, ⛔ O9 окно): `./scripts/backup.sh && ./scripts/restore_check.sh` — «OK, campaigns=N».

Исполнитель: **deployer**. Зависимости: нет (O9 — к D2). **2 ч.** Ветка `v2/ops`.

### Понедельник — приёмка (ветка `release` = `main`)

#### D1 — Золотой набор на VPS, разметка, исправления
5 RE + 3 инвестора (включая ≥ 1 пост-ссылку) + 1 both из O7; `scripts/eval_campaign.sh` на каждую; разметка в
`docs/v2/GOLDEN_SET.md`; найденные дефекты → мини-задачи (≤ 1 ч каждая) в соответствующий поток, PR, мерж.
**analyst** + **reviewer**; deps: A1, A2, A3, A5, A6, B1, B2, B4, C2, C3, O7, **O1, O2** (live). **5 ч.** ⛔ деньги
(потолок на D1: `CAMPAIGN_BUDGET_USD` × 9 ≤ $30 — подтвердить с O10).

#### D2 — Деплой, включение флагов, проверка алертов
Блок команд по скиллу `deploy-vps`: бэкап → `BRANCH=main ./scripts/update.sh` → миграции 047–048 → флаги
(`ACQUISITION_MODE=apify`, порталы, `FEATURE_APIFY_FACEBOOK`, `APIFY_FACEBOOK_COMMENTS_ENABLED`, `LEADS_PROVIDER`,
`CAMPAIGN_JOIN_NOTICES`, `ARCHITECT_PROVIDER`, `WATCHDOG_ENABLED`, модели из A5, `OWNER_ALERT_CHAT_ID`) → один
проверочный вызов на каждом LLM-этапе → `docker compose kill analysis-worker` → алерт → `up -d`. **deployer**;
deps: D1, O4, O8, O9, O10. **2 ч.** ⛔ деплой.

#### D3 — `HANDOFF.md`, `ROLLBACK.md`, инструкция клиенту
`docs/v2/ROLLBACK.md` — таблица «часть → переменная → строка отката» (DoD-6), `docs/v2/HANDOFF.md` (сервисы,
флаги, где смотреть расходы/алерты, как читать `GOLDEN_SET.md`), `docs/v2/CLIENT_GUIDE.md` (как ставить задачу
голосом/текстом/ссылкой, что значат карточки и отчёт). **architect**; deps: D2. **2 ч.**

---

## 2. Граф зависимостей

```
O3 ──► A3 ─────────────────────────────┐
O1,O2,O6 ──► A4 ──► (B3 live)          │
A1 ───────────────────────────────────►│
A2 (047) ─────────────────────────────►│
A5 ───────────────────────────────────►│
A6 ───────────────────────────────────►├──► D1 ──► D2 ──► D3
B1 (048) ──► B2 ──────────────────────►│      ▲      ▲
   ├──────► B3                         │     O7   O4,O8,O9,O10
   └──────► B4 ──► C2 ────────────────►│
O3 ──► B5                              │
B1 ──► B6 (cut)                        │
O4 ──► C1-lite (не блокирует C2)       │
C3 ───────────────────────────────────►│
C4 ───────────────────────────────────►┘
O4 ──► C5 (cut)        O5 ──► C1-full (cut)
```

Критический путь: **B1 (9) → B4 (5) → C2 (4) → D1 (5) → D2 (2) → D3 (2) = 27 ч** последовательно. B1 начинается
первым в субботу утром; при трёх параллельных исполнителях всё остальное укладывается рядом.

Разблокирует больше всего: **B1** (B2, B3, B4→C2, B6, D1) — приоритет 1; затем A1 (D1, режимы), A2 (D1, B3-live).

## 3. План по дням (≤ 3 исполнителя одновременно, каждый в своём worktree)

| День | Слот 1 | Слот 2 | Слот 3 | Часы | ⛔ к этому дню |
|---|---|---|---|---|---|
| **Пт** | A1 builder (4) → A2 builder+db (5) | A5 analyst (1.5) → A6 analyst (2) → B4-отчёт analyst (3) | B5-код sources (6) → A3 sources (2, после O3) | 23.5 | O1, O2, O3, O6, O10 |
| **Сб** | B1 builder+sources+db (9) | B5-live sources (1) → B3 sources (3) → B2 builder (2) | C3 deployer+builder (6) → C4 deployer (2) | 23 | O7, O4 |
| **Вс** | B4-runner builder (2) → C2 builder (4) | C1-lite builder+analyst (5) | A4 (1, владелец) → reviewer: PR всех потоков, мерж в `main` | 12 + ревью | O5 (если C1-full), O8, O9 |
| **Пн** | D1 analyst+reviewer (5) | D2 deployer (2) → D3 architect (2) | исправления по D1 | 9 | деплой |

Сумма обязательных: A 15.5 + B 25 + C 17 + D 9 = **66.5 ч** работы субагентов + ревью (≈ 0.5 ч на PR × 7) ≈ 70 ч.
Три параллельных слота × 4 дня дают ≈ 25 ч/день → укладывается с запасом ≈ 25 ч на исправления после D1 и на
правки мапперов по живой выдаче. Опциональные B6 (6), C5 (8), C1-full (8) — не планируются.

**Порядок мержа и номера миграций.** 047 (A2, `v2/acquisition`) мержится раньше 048 (B1, `v2/investors`).
Если порядок поменяется — db-агент перенумеровывает перед мержем (имя файла + оба списка `apply_migrations.sh`).
Ветки от `main`; один PR на поток; пересечения по файлам: `campaign/settings.py` (A1, B1/B2, B4), `.env.example`
и `docker-compose.yml` (все) — мержить по одному, после каждого — `git merge main` в остальные.

## 4. Линия отсечения (если отстаём к утру воскресенья)

Режем в порядке: **B6 → C5 → C1-full → A2b (кнопка «Вступил», остаётся уведомление) → B5 Milanuncios → C1-lite
(интервьюер остаётся на OpenRouter + Sonnet 5.5 через A5) → B3 (группы инвесторов читаются через discovery как
сейчас) → B2**. Не режем: A1, A2a, A3, A5, A6, B1, B4, C2, C3, C4, D1–D3.

## 5. Остановки владельца (⛔) и что нужно

| Узел | Что | Блокирует | Когда |
|---|---|---|---|
| O1 | Письменное решение клиента по `COMPLIANCE.md` §4 (Apify, группы и комментарии) | A4, B3-live, D1, D2 | Пт |
| O2 | Apify: тариф, лимит трат в Console (например $30/мес) | A3, A4, B5-live, D1 | Пт |
| O3 | «Ок» на живые сэмплы ≤ $0.05 (A3) и ≤ $0.02 × 3 (B5) | A3, B5-live | Пт |
| O4 | `ANTHROPIC_API_KEY` в `.env` VPS (лимит трат в Console); в чат не присылать | C1-lite live, D2 | Сб |
| O5 | «Ок» на зависимость `claude-agent-sdk` — **только если решим делать C1-full** | C1-full | — |
| O6 | Группы FB: недвижимость 10–20, инвесторы 5–10; готовность вступать в закрытые | A4, B3 | Пт |
| O7 | Золотой набор G2–G9 + 2–3 ссылки на посты с ≥ 30 комментариями | D1 | Сб |
| O8 | Telegram chat id для алертов (`OWNER_ALERT_CHAT_ID`) | D2 | Вс |
| O9 | Окно перезагрузки VPS | D2 | Вс |
| O10 | Бюджет кампании по умолчанию (сейчас $5) и потолок на D1 (≈ $30) | D1, D2 | Пт |
| **O11 (новый)** | Watchdog: разрешить `/var/run/docker.sock:ro` контейнеру `watchdog` или делать heartbeat-таблицу (+1 ч) | C3 | Сб |
| **O12 (новый)** | C1: lite (Claude API напрямую, без новой зависимости) или full (Agent SDK, +3 ч, O5) | C1 | Сб |

## 6. Что нужно подтвердить перед шагом 2

1. «Ок» на этот план (или правки).
2. Ответы по O11 и O12 (рекомендации: docker.sock read-only; C1-lite).
3. Среда для субагентов: в контейнере сессии Python 3.13 без pytest — первым шагом ставлю `.venv` (3.11/uv) и
   проверяю `ruff check .` и `pytest -q` зелёные на `main` до любых правок.
- [x] H5 (facebook-runner egress, закреплённый билд Apify) и H6 (`APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY=0` = без дневной квоты) — хотфиксы 16–17.10.
