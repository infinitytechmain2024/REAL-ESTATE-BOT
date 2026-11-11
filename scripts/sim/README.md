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
  инвесторам, `CAMPAIGN_BUDGET_USD=0.8`; остальное — умолчания `docker-compose.yml`. `env` персоны перекрывает любое.
  Процесс работает из `sim/`: `.env` репозитория стенд не читает.
- **Facebook**: `SIM_FACEBOOK=none` (по умолчанию) — группы, названные в задаче, идут в Apify как в проде; без них
  Facebook ничего не добавляет, кампания идёт дальше по другим источникам. `SIM_FACEBOOK=browser` — поиск групп через
  Browser Session Manager: страницы из `sim/browser/<хэш url>.json`, иначе пустая выдача (см. «Находки»).
- **Режимы**: по умолчанию прогон собирает все pending прохода (бот получает отказ и идёт дальше, как при сбое
  модели); `--step` — стоп на первом же. Pending с `"speculative": true` появился после другого неотвеченного: его
  вход мог сложиться из запасного пути, на него можно не отвечать, пока не ответили на остальные. Запуски Apify после
  неотвеченного LLM-вызова в pending не пишутся (только счётчик `Apify not asked`): их вход может быть от fallback, а
  запись стоит денег.

## Ответы

Все пути — относительно `sim/`. Отвеченный pending можно удалить; следующий прогон всё равно стирает pending своего
`run` и пишет заново то, чего ещё нет.

| Ждёт | Файл запроса | Куда ответ | Формат ответа |
|---|---|---|---|
| LLM | `pending/<ключ>.json`: `stage`, `caller`, `model`, `system`, `user`, `schema` | `cassette/<ключ>.json` | `{"content": "<JSON-ответ модели строкой>"}` или `{"tool_call": {"name": "...", "input": {...}}}`; `{"error_status": 503}` — записанный сбой |
| Apify | `pending_apify/<актор>__<хэш>.json`: `actor`, `input`, `params` | `apify/<актор>/<хэш>.json` (`<актор>` в форме `owner~name`) | `[<строки датасета>]` или `{"items": [...], "usage_usd": 0.0}`; `apify/<актор>/_default.json` — для любого входа |
| пользователь | `pending_answers/<run>.json`: `question`, `buttons`, `dialogue` | `answers/<run>.json` | `{"answers": [{"text": "до 3 млн евро"}, {"press": "<текст или data кнопки>"}, ...]}` — дописать следующий |

- **Ключ LLM** = sha256 от (провайдер, модель, сообщения с system, схема `response_format` или `tools`) после
  нормализации: UUID и время заменены, поэтому повтор в другой день попадает в те же ответы. Ответ отдаётся как
  OpenRouter chat completion с `usage.cost = 0`; в `campaign_costs` такие строки идут с провайдером
  `claude_cassette`. Interviewer на Anthropic API (`ARCHITECT_PROVIDER=anthropic` в `env` персоны) — тот же ключ и
  формат, ответ отдаётся `tool_use`-блоком.
- **Ключ Apify** = sha256 входа актора. Жизненный цикл эмулируется: POST → `RUNNING`, первый GET → `SUCCEEDED`,
  страницы датасета по `offset/limit`, abort. Стартовый набор — живые фикстуры порталов:
  `.venv/bin/python -m scripts.sim.fixtures` отвечает на pending порталов (Madrid, совпадающие сделка и тип) файлами
  `tests/fixtures/portals_live/`, остальное печатает как `left`. Новые записи — оркестратор через MCP, в бюджете.
- **Пользователь**: ответ на i-й вопрос — i-й элемент `answers`. Кнопку можно назвать текстом («Не важно») или data
  (`task:skip`). На карточке «Проверьте задачу» лишний ответ правит задачу, `{"press": "task:launch"}` или конец
  списка — запуск.
- Прочие хосты (SearXNG, сайты): `http/<host>/<хэш>.json` (`{"status": 200, "json": ...}` или `{"text": ...}`), если
  записано; иначе SearXNG находит ничего, сайт отвечает 503, запрос попадает в `offline` результата.

## Цикл оркестратора

1. `python -m scripts.sim.run --persona sim/personas/<p>.yaml --run <id>`.
2. Код `3`: ответить на pending — `pending_answers/<id>.json` агенту-пользователю (дописать `answers/<id>.json`),
   каждый `pending/*.json` агенту в роли модели этапа (`stage`; ответ строго по `schema` и формату из `system`),
   Apify — `python -m scripts.sim.fixtures`, остаток — запись прогона через MCP с `maxTotalChargeUsd` не выше
   `params.maxTotalChargeUsd`. Сначала не-`speculative`.
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
