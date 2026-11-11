# Стенд симуляции пользователя (T1)

Один процесс прогоняет задачу пользователя как в проде: интервью (`TaskIntake` + интервьюер) → «Проверьте задачу» →
Orchestra → кампания (web-этап с порталами Apify, соцэтап Apify, reach, leads, analysis-worker, раннер) → карточки,
лиды, отчёт. Без сети, без ключей, без Docker и Telegram. На LLM-запросы отвечают агенты Claude через файлы-кассеты,
выдачи Apify — записанные прогоны. Продовый код не меняется: стенд подменяет только внешний мир.

Инструмент разработки: в образы не попадает, флага не требует. `sim/` целиком в `.gitignore` (там ПДн живых выдач);
в git — только `scripts/sim/` и синтетика `tests/sim/fixtures/`.

## Postgres

Выбран локальный кластер PostgreSQL 16 (memory-store'ов нет у analysis-worker, а раннер работает на SQL): данные в
`sim/pg/`, `127.0.0.1:54329`, trust, база `sim_test`. Под root `initdb` и сервер запускаются от `postgres`.

```bash
.venv/bin/python -m scripts.sim.db start    # создать (один раз) и запустить, печатает URL
.venv/bin/python -m scripts.sim.db stop
```

`run` поднимает кластер сам. Каждый прогон начинается с пустой базы: схемы удаляются, применяются все миграции
`bot/services/db/migrations` (тот же список, что в `scripts/apply_migrations.sh`). `SIM_DATABASE_URL` — другая база
(имя обязано кончаться на `_test`: она стирается).

## Прогон

```bash
.venv/bin/python -m scripts.sim.run --persona <файл|текст> --run <id> [--mode real_estate|investors|both|post_link] [--step]
```

Печатает статус, сколько ждёт ответов LLM / Apify / пользователя и где они лежат; пишет `sim/runs/<id>/result.json`.
Код выхода: `0` готово, `3` ждёт ответов, `4` застряло (интервью не дошло до «Запустить», кампания не закончилась).

- **Персона** — JSON или YAML: `{"mode": "investors", "task": "<первое сообщение>", "voice": false,
  "confirm_card": false, "env": {"APIFY_LINKEDIN_ENABLED": "true"}}`; или просто текст первого сообщения (режим из
  `--mode`, по умолчанию `real_estate`). `post_link` — режим инвесторов, задача = ссылка на пост. `voice: true` —
  первое сообщение приходит голосом (расшифровка = `task`). `confirm_card: true` — карточка «Проверьте задачу» тоже
  ждёт ответа пользователя (иначе запускается как показана).
- **Окружение** — `harness.BASE_ENV`: ключи-заглушки `sim-…`, `ACQUISITION_MODE=apify`, порталы Idealista
  (igolaizola), Fotocasa, Habitaclia, Milanuncios, комментарии Facebook через Apify, `INTAKE_BOTH_MODE`, отчёт по
  инвесторам, `CARD_EXTRA_FIELDS=true` (как на проде, `docs/v2/CLIENT_DECISIONS.md:76`: без него в карточке участка
  нет «Площадь»), `CAMPAIGN_BUDGET_USD=0.8`; остальное — умолчания `docker-compose.yml`. `env` персоны перекрывает
  любое. Расхождения с продом — ниже, «BASE_ENV и прод».
  После `env` персоны всегда ставится `WEB_SEARCH_IMPERSONATE=off` (`harness.FORCED`), а `CurlTransport` веб-этапа
  на время прогона заменён httpx-транспортом: curl_cffi шёл бы мимо httpx и мимо запрета сокетов. Все `*_PROXY` /
  `*_proxy` на время прогона убираются, затем возвращаются; так же сохраняются и возвращаются рабочий каталог,
  состояние `random`, `uuid.uuid4` и ledger `costs` вызывающего (`harness.sim_process`). Прогон работает из `sim/`
  (chdir в `sim_process`): `.env` репозитория настройки сервисов не читают — тесты тоже (там ещё и `chdir` во
  временный каталог).
- **Facebook**: `SIM_FACEBOOK=none` (по умолчанию) — группы, названные в задаче, идут в Apify как в проде; без них
  Facebook ничего не добавляет, кампания идёт дальше по другим источникам. `SIM_FACEBOOK=browser` — поиск групп через
  Browser Session Manager: страницы из `sim/browser/<хэш url>.json`, иначе пустая выдача (см. «Находки»).
- **Режимы**: по умолчанию прогон собирает все pending прохода (бот получает отказ и идёт дальше, как при сбое
  модели); `--step` — стоп на первом же. Pending с `"speculative": true` появился после другого неотвеченного: его
  вход **возможно** зависит от неотвеченного (мог сложиться из запасного пути), а не точно fallback: флаг ставится,
  если в проходе уже есть любой неотвеченный LLM/Apify, даже не связанный с этим вызовом (`scripts/sim/cassette.py`,
  `speculative`). Сначала отвечать на остальные: после их ответов он либо повторится тем же ключом (ответ
  пригодится), либо сменится новым. Запуски Apify после
  неотвеченного LLM-вызова в pending не пишутся (только счётчик `Apify not asked`): их вход может быть от fallback, а
  запись стоит денег.

## Ответы

Все пути — относительно `sim/`. Отвеченный pending можно удалить; следующий прогон всё равно стирает pending своего
`run` и пишет заново то, чего ещё нет.

| Ждёт | Файл запроса | Куда ответ | Формат ответа |
|---|---|---|---|
| LLM | `pending/<ключ>.json`: `stage`, `caller`, `model`, `system`, `user`, `schema` | `cassette/<ключ>.json` | `{"content": "<JSON-ответ модели строкой>"}` или `{"tool_call": {"name": "...", "input": {...}}}`; `{"error_status": 503}` — записанный сбой |
| Apify | `pending_apify/<актор>__<хэш>.json`: `actor`, `input`, `params` | `apify/<актор>/<хэш>.json` (`<актор>` в форме `owner~name`) | `[<строки датасета>]` или `{"items": [...], "usage_usd": 0.0}`; `apify/<актор>/_default.json` — для любого входа |
| пользователь | `pending_answers/<run>.json`: `question`, `buttons`, `keyboard`, `dialogue` | `answers/<run>.json` | `{"answers": [{"text": "до 3 млн евро"}, {"press": "<текст или data кнопки>"}, ...]}` — дописать следующий |

- **Ключ LLM** = sha256 от (провайдер, модель, сообщения с system, схема `response_format` или `tools`) после
  нормализации: заменены UUID и только «сейчас» стенда — дата или метка времени, чей день сегодня, день старта
  прогона или день виртуальных часов (`cassette.mark_now`). Даты из данных (дата объявления) остаются в ключе.
  Повтор в другой день попадает в те же ответы; исключение — объявление, датированное днём записи. Ответ отдаётся как
  OpenRouter chat completion с `usage.cost = 0`; в `campaign_costs` такие строки идут с провайдером
  `claude_cassette`. Interviewer на Anthropic API (`ARCHITECT_PROVIDER=anthropic` в `env` персоны) — тот же ключ и
  формат, ответ отдаётся `tool_use`-блоком.
- **Ключ Apify** = sha256 входа актора. Жизненный цикл эмулируется: POST → `RUNNING`, первый GET → `SUCCEEDED`,
  страницы датасета по `offset/limit`, abort; в датасете не больше `maxItems` запуска. Стартовый набор — живые фикстуры порталов:
  `.venv/bin/python -m scripts.sim.fixtures` отвечает на pending порталов (Madrid, совпадающие сделка и тип) файлами
  `tests/fixtures/portals_live/`, остальное печатает как `left`. Строки, которые не прошли бы фильтры входа актора
  (`fixtures.FILTERS`: `minPrice`, `maxPrice`, `minSize` у Idealista, `minArea` у Habitaclia, Pisos, Yaencontre — как
  их ставит `bot/web_search/sources/portals.py`; у Milanuncios фильтров во входе нет), в запись не попадают: стенд не
  отдаёт того, чего прод не получил бы. Строка без значения (у Habitaclia `areaSqm` пуст, площадь только в тексте)
  остаётся: что с ней делает портал, неизвестно. Новые записи — оркестратор через MCP, в бюджете.
- **Пользователь**: ответ на i-й вопрос — i-й элемент `answers`. Кнопку можно назвать текстом («Не важно») или data
  (`task:skip`). `buttons` — inline-кнопки под сообщением, `keyboard` — ряды нижней клавиатуры, которая сейчас на
  экране (у «Проверьте задачу» — `▶️ Запустить`, `✏️ Изменить`, `❌ Отмена`, `bot/control_plane/intake.py:310`);
  в `dialogue` у сообщения бота `keyboard` есть, только когда оно её ставит или убирает (`[]`). Нажатие клавиши —
  `{"press": "✏️ Изменить"}` или без значка (`"Изменить"`): стенд отправляет подпись текстом, как Telegram. Inline-
  кнопка с тем же текстом важнее клавиши. `{"press": "task:launch"}` / `"Запустить"` на карточке — как раньше,
  inline-callback; `"▶️ Запустить"` — запуск через клавиатуру. На карточке «Проверьте задачу» лишний ответ правит задачу, `{"press": "task:launch"}` или конец
  списка — запуск.
- Прочие хосты (SearXNG, сайты): `http/<host>/<хэш>.json` (`{"status": 200, "json": ...}` или `{"text": ...}`), если
  записано; иначе SearXNG находит ничего, сайт отвечает 503, запрос попадает в `offline` результата.

## BASE_ENV и прод

Прод — `docs/v3/P0_CHECKLIST.md` (флаги v2, которые владелец включает в P0). Совпадают: `ACQUISITION_MODE=apify`
(б1), Fotocasa (б1), `APIFY_FACEBOOK_COMMENTS_ENABLED=true` и `LEADS_PROVIDER=apify` (г), `CAMPAIGN_INVESTOR_REPORT`
и `INTAKE_BOTH_MODE` (д), `CARD_EXTRA_FIELDS=true` (з), `ARCHITECT_PROVIDER=openrouter` (е пропущен),
`CAMPAIGN_BUDGET_USD=0.8` (на время прогона G1–G9, п. 6). Расходятся (стенд не менялся, кроме `CARD_EXTRA_FIELDS`):

Флаги Q4, Q5, Q8, Q9, Q10 (`DEDUP_WITHOUT_ADDRESS`, `IDEALISTA_SORT_BIGGEST`, `RELEVANCE_SOFT_WISHES`, `SEARCH_QUERY_HYGIENE`, `ANALYSIS_LOCATION_FROM_TEXT`) в `BASE_ENV` включены заранее: пока их ветки не смержены в `main`, бот их игнорирует.

| Ключ | Стенд | Прод по P0 | Следствие |
|---|---|---|---|
| `OPENROUTER_*_MODEL` | умолчания compose (`claude-sonnet-4.5`, `gpt-4o-mini`) | (а): `claude-sonnet-5.5`, `claude-haiku-5.5`, `claude-opus-5.5` (final) | модель — часть ключа кассеты; ответы кассеты пишет агент, не эта модель. Сменить — новые ключи у всех pending |
| `APIFY_IDEALISTA_ENABLED`, `APIFY_IDEALISTA_PROVIDER=igolaizola`, `APIFY_HABITACLIA_ENABLED`, `APIFY_MILANUNCIOS_ENABLED` | `true` | (б2)/(б3) — только с «ок» владельца по O3, иначе `false` (откат — `axlymxp`) | на проде без O3 порталов меньше: только Fotocasa |
| `APIFY_YAENCONTRE_ENABLED`, `APIFY_PISOS_ENABLED` | `false` (умолчание) | (б2)/(б3) с «ок» O3 — `true` | при «ок» O3 на проде порталов больше |
| `APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY` | `40` (умолчание) | (г): `0` (без квоты) | на стенде квота есть, на сотни комментариев за день не влияет |
| `APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST` | `1.0` (умолчание) | `0.5` на время G1–G9 (п. 6), потом `1.0` | — |
| `CAMPAIGN_JOIN_NOTICES` | `false` (умолчание) | (д): `true` | стенд не показывает «Вступил ✅» по закрытым группам |
| `FEATURE_APIFY_FACEBOOK` (+ `APIFY_FACEBOOK_*`) | `false` | A4 (п. 4): `true` на время проверки, дальше — по решению владельца | группы Facebook через Apify на стенде — только названные в задаче (`SIM_FACEBOOK`) |
| `WATCHDOG_*` | — | (ж): `true` | к кампании не относится |
| `APIFY_MIN_LISTINGS` | `5` (умолчание) | (б1): `5` | совпадает |

## Деньги

Стенд не платит. Каждый живой запуск Apify, сделанный оркестратором через MCP для стенда, записывается в
`sim/spend.jsonl`: `scripts.sim.spend.record_spend(actor, run_id, usd, note)` или
`python -m scripts.sim.spend add --actor apify/instagram-scraper --run-id <id> --usd 0.12 --note G7`.
`python -m scripts.sim.spend [--limit 10]` печатает сумму по акторам и выходит с кодом 1, если сумма выше потолка
($10 на весь цикл T3): тогда новых живых запусков нет.

## Цикл оркестратора

1. `python -m scripts.sim.run --persona sim/personas/<p>.yaml --run <id>`.
2. Код `3`: ответить на pending — `pending_answers/<id>.json` агенту-пользователю (дописать `answers/<id>.json`),
   каждый `pending/*.json` агенту в роли модели этапа (`stage`; ответ строго по `schema` и формату из `system`),
   Apify — `python -m scripts.sim.fixtures`, остаток — запись прогона через MCP с `maxTotalChargeUsd` не выше
   `params.maxTotalChargeUsd`, затем `record_spend` и `python -m scripts.sim.spend` (код 1 — стоп). Сначала
   не-`speculative`.
3. Повторить 1. Всё отвеченное воспроизводится детерминированно, прогон доходит до следующих вопросов.
4. Код `0`: `runs/<id>/result.json` готов для судьи.

## result.json

`status`, `stop_reason`, `persona`, `dialogue` (всё интервью), `questions` (вопрос → ответ), `check_card`
(«Проверьте задачу»), `campaign` (состояние, план, spec), `user_saw` (все сообщения раннера в чате, с правками и
удалениями), `cards` (отправленные карточки: текст, ссылка, payload фактов), `findings` (все, с bucket и причиной),
`leads` (лиды из комментариев, с карточкой, если отправлен), `reach_contacts` (инвесторы из reach, с карточкой,
если отправлен), `report` (отчёт по поиску), `campaign_costs` и
`costs_total_usd`, `pending`, `llm_calls` (вызовы по этапам), `offline`, `unhandled`, `errors` (`stages` — исключения
этапов, `logged` — предупреждения и ошибки бота по модулям). Время вырезано; id детерминированы (UUID из
последовательности `sim.gen_random_uuid`, `uuid.uuid4` процесса — счётчик), поэтому повтор даёт тот же файл.
Исключение — тексты бота относительно сегодняшней даты («последний комментарий сегодня»): в другой день они другие.

## Как устроено (швы)

Продовый код не менялся; всё подставляется снаружи:

- `cassette.offline` подменяет `httpx.AsyncHTTPTransport.handle_async_request` на `SimNet.handle` (любой клиент бота
  без своего транспорта идёт туда) и запрещает сокеты не на loopback; `WEB_SEARCH_IMPERSONATE=off`, иначе curl_cffi
  шёл бы мимо httpx.
- Раннер, web-этап, reach, leads, post-leads и Apify-задачи собираются фабриками `bot/campaign/runner.py`
  (`_web_stage`, `_reach_worker`, …) как в `main()`; `CampaignRunner(now=)` — виртуальные часы: простой раунд
  двигает их на 20 мин, чтобы закончились ожидания дренажа. Discovery — через параметр `discovery=` раннера.
- Паузы `sleep`/`_sleep` объектов этапов (10 с биллинга Apify, бэкоффы) заменяются мгновенными.
- Тесты: `tests/sim/test_harness.py`; синтетическая модель `tests/sim/model.py` (параметр `responder` у `Bench`)
  отвечает по данным вызова и пишет кассету, второй прогон идёт только по кассете.

## Находки (прод, по ходу T1; не исправлялись)

1. `ACQUISITION_MODE=apify` без `BROWSER_SESSION_API_TOKEN` и без групп в задаче: discovery отказывает
   `no_groups_no_browser` (`bot/campaign/discovery.py:704-705`), раннер отвечает `NO_GROUPS`
   (`bot/campaign/runner.py:478-479`) и кампания навсегда остаётся `planned`: ни дренажа, ни отчёта.
2. С браузером: пустой поиск групп Facebook сразу ставит кампании `completed / no_active_groups`
   (`bot/campaign/discovery.py:739-742`), не дожидаясь сайтов и анализа: порталы успевают только запуститься, отчёт
   «Объявлений для оценки не получено». Поэтому по умолчанию `SIM_FACEBOOK=none`.
3. Пост по ссылке: после отправленного лида и отчёта последней строкой приходит «Пока ничего подходящего не нашёл.»
4. После сбоя ИИ-проверки раннер не спрашивает модель минуту (`runner.py`, `_relevance_paused_until`): в режиме
   сбора pending ревьюера приходит по одному за проход.
