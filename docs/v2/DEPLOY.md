# Выкатка v2 на VPS (D2): runbook владельца

Все команды выполняются на VPS в `/opt/real-estate-bot` под root. Я (разработчик) на сервер не хожу: вы запускаете блоки
по порядку и сверяете с «Норма». Если результат не совпал с нормой, дальше не идти, выполнить откат этого блока.
Источники: `.env.example`, `docker-compose.yml`, `scripts/*.sh`, `docs/v2/OPS.md`, `docs/v2/MODELS.md`,
`docs/apify_adapter.md`, `docs/apify_portals.md`, skill `deploy-vps`.

Принципы:

* Каждый флаг по умолчанию выключен. Включаем по одному блоку, после каждого проверка; откат блока одной строкой.
* Откат флага = дописать в конец `.env` прежнее значение (`echo 'KEY=old' >> .env`) и тот же `up -d`.
  Docker Compose берёт **последнюю** строку ключа в `.env`. Проверка любого значения: `docker compose --env-file .env config | grep KEY`.
* Секреты (`APIFY_TOKEN`, `ANTHROPIC_API_KEY`, токены) в чат и в логи не копировать. В блоках ниже `<...>` вписываете сами.
* Прежнее значение смотрите до включения: `grep -E '^KEY=' .env` (если строки нет, действует умолчание из таблицы раздела 5).

## 0. Предусловия

Без этого не начинать соответствующий блок.

| Что | Нужно для | Статус |
|---|---|---|
| O2: `APIFY_TOKEN` уже в `.env`, в Apify Console задан месячный лимит трат (например $30) | блоки (б), (в), (г) | проверить: `grep -c '^APIFY_TOKEN=.' .env` даёт `1` |
| O10: бюджет кампании (`CAMPAIGN_BUDGET_USD`, сейчас 5) и потолок на тестовый прогон (около $30 суммарно) | все проверочные кампании | решить заранее |
| O1: письменное решение клиента по `COMPLIANCE.md` §4 (отправка содержимого групп в Apify) | блоки (в), (г): ⛔ без него не включать | есть / нет |
| Закреплённый билд актора Facebook-групп (Apify Console, страница актора `apify/facebook-groups-scraper`, вкладка Builds, номер вида `0.1.23`, **не** `latest`) | блок (в) | записать |
| То же для `apify/facebook-comments-scraper` | блок (г) | записать |
| O4: `ANTHROPIC_API_KEY` (лимит трат в Anthropic Console); перед включением сверить id модели `claude-sonnet-5-5` в документации Anthropic и цену в `bot/utils/costs.py` | блок (е): ⛔ | есть / нет |
| O8: ваш Telegram chat id для алертов | блок (ж) | число |
| O7: ссылка на пост с 30 и более комментариями, одна открытая группа для первого прогона FB | блоки (в), (г) | есть |
| Живые сэмплы B5 по порталам (O3, до $0.02 каждый) подтверждены | порталы Yaencontre и Idealista в блоке (б) | Fotocasa подтверждён живым запуском 13.10, остальные по схеме актора |
| O9: окно на перезагрузку VPS | раздел 6 | дата |
| Проверить id моделей на openrouter.ai/models и цены на openrouter.ai/pricing: `anthropic/claude-sonnet-5.5`, `anthropic/claude-haiku-5.5`, `anthropic/claude-opus-5.5` (в `docs/v2/MODELS.md` они записаны по памяти) | блок (а) | сверено / нет |

Если id на openrouter.ai отличается, подставляйте точный id в `.env`, а мне сообщите: поправлю `PRICES` в `bot/utils/costs.py`.

## 1. Бэкап (обязательно перед обновлением)

```bash
cd /opt/real-estate-bot
set -o pipefail
chmod 700 /root/backups 2>/dev/null || true
./scripts/backup.sh && ls -lh /root/backups | tail -3
./scripts/restore_check.sh
```

Норма: `OK: /root/backups/bot-ГГГГММДД-ЧЧММСС.sql.gz (N bytes)`, размер заметно больше 10 КБ (дамп <= 10 КБ скрипт сам
удаляет и выходит с кодом 1), затем `OK, campaigns=N`. При любой ошибке на этом шаге остановиться.

Копия настроек (нужна для отката любого флага целиком):

```bash
cp .env .env.bak.$(date +%F-%H%M) && ls -l .env.bak.* | tail -1
```

## 2. Обновление кода, миграции, состояние

```bash
cd /opt/real-estate-bot
git status --short --untracked-files=no          # должно быть пусто, иначе update.sh остановится (код 3)
git rev-parse --short HEAD                       # запомнить: это коммит для отката кода
BRANCH=main ./scripts/update.sh 2>&1 | grep -vE '^ =>|NOTICE|Already applied'
```

`update.sh` по шагам: `git merge --ff-only` -> `pull` + `build --pull` -> `scripts/apply_migrations.sh` ->
`up -d --force-recreate` -> `image prune` -> `ps` и `Caddy healthz`. Тома БД не удаляются.

Норма в выводе: строки merge-коммитов, `Applied: 047_campaign_group_join.sql`, `Applied: 048_campaign_post_reads.sql`,
`Applied: 049_campaign_comment_reads_run_id.sql` (или `Migrations are current`, если уже применены), `Caddy healthz: OK`.

Проверка (одной серией):

```bash
cd /opt/real-estate-bot
docker compose --env-file .env exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select filename, applied_at from public.schema_migrations where filename >= '"'"'046'"'"' order by filename"'
docker compose --env-file .env ps
sleep 70   # start_period healthcheck 60 с
docker compose --env-file .env ps --format 'table {{.Service}}\t{{.Status}}'
docker compose --env-file .env logs --since=5m | grep -iE "traceback|critical|error" || echo "ошибок нет"
curl -fsS http://127.0.0.1:8080/healthz && echo healthz ok
```

Норма: в списке строки 046, 047, 048, 049; у каждого сервиса `Up ... (healthy)` (у `searxng`, `browser`, `campaign-runner`,
`analysis-worker`, `reduction-worker` тоже есть healthcheck); нет `unhealthy` и `Restarting`; `healthz ok`. Строки с
`error` в логах разберите: безобидные (например `error` внутри текста поста) отличайте от `Traceback`. Если есть
Traceback, перейти к разделу 5 (откат кода).

Сервисы за профилями (`collector`, `agent-reach`, `scrapling`, `analysis`, `ops`) поднимаются только если профиль есть в
`COMPOSE_PROFILES`. Что включено сейчас: `grep -E '^COMPOSE_PROFILES' .env || echo "профили не заданы"`.

Короткая функция для SQL ниже (в каждой новой SSH-сессии заново):

```bash
cd /opt/real-estate-bot
psqlc() { docker compose --env-file .env exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' sh "$1"; }
```

## 3. Включение флагов по этапам

Порядок важен: от дешёвого и обратимого к платному. После каждого блока смотрите «Проверка», только потом следующий блок.
Все блоки начинаются с `cd /opt/real-estate-bot`. Служебная проверка «LLM-ошибки за последние полчаса» (используется в (а)):

```bash
psqlc "select stage, code, item, count(*) from campaign_costs where kind='error' and created_at > now() - interval '30 minutes' group by 1,2,3 order by 4 desc"
```

### (а) Модели v2 (`docs/v2/MODELS.md`)

Меняются только значения `OPENROUTER_*_MODEL`. Строки в `.env.example` закомментированы (`# v2 ...: KEY=значение`):
берём значение справа от двоеточия. Три подблока по сервисам, на каждом один проверочный вызов, чтобы ошибку 400 (неверный
id модели) увидеть сразу, а не по пустой кампании.

Как выглядит ошибка модели:

* analysis-worker: в логе `analysis.request_refused 400` (или `404`) и `analysis.model_unavailable http_4xx`; пост
  закрывается с `error`. Смотреть: `docker compose --env-file .env logs --since=10m analysis-worker | grep -E "request_refused|model_unavailable|invalid_model"`.
* campaign-runner: в `campaign_costs` строки `kind='error'`, `stage='llm'`, `code='http_400'`, `item` = id модели. Смотреть служебным запросом выше.
  Плюс `docker compose --env-file .env logs --since=10m campaign-runner | grep -iE "error|traceback|http_4"`.
* telegram (интервью): бот не задаёт вопрос или отвечает встроенными правилами; в логе `telegram.intake.interview_unreadable`. Смотреть:
  `docker compose --env-file .env logs --since=10m telegram | grep -iE "interview|error|traceback"`.

Запомните прежние значения: `grep -E '^OPENROUTER_.*_MODEL=' .env` (если строк нет, прежние значения в таблице раздела 5).

**(а1) Интервью (telegram).**

```bash
cat >> .env <<'ENV'
OPENROUTER_INTERVIEW_MODEL=anthropic/claude-sonnet-5.5
ENV
docker compose --env-file .env up -d telegram
```

Проверка: написать боту новую задачу текстом, получить уточняющий вопрос от модели; `logs --since=5m telegram | grep -iE "interview|error|traceback"`
не показывает `interview_unreadable`. Откат:

```bash
echo 'OPENROUTER_INTERVIEW_MODEL=anthropic/claude-sonnet-4.5' >> .env && docker compose --env-file .env up -d telegram
```

**(а2) Анализ страниц (analysis-worker).**

```bash
cat >> .env <<'ENV'
OPENROUTER_ANALYSIS_MODEL=anthropic/claude-sonnet-5.5
ENV
docker compose --env-file .env up -d analysis-worker
# если в COMPOSE_PROFILES есть analysis, ещё: docker compose --env-file .env up -d analysis-pipeline
```

Проверка: дождаться одного разбора поста или страницы (кампания из (а3) или любой новый пост), затем
`logs --since=10m analysis-worker | grep -E "request_refused|model_unavailable"` пусто. Откат:

```bash
echo 'OPENROUTER_ANALYSIS_MODEL=anthropic/claude-sonnet-4.5' >> .env && docker compose --env-file .env up -d analysis-worker
```

**(а3) Кампания: план, отбор, запросы web, отчёт, лиды, reach (campaign-runner).**

```bash
cat >> .env <<'ENV'
OPENROUTER_PLAN_MODEL=anthropic/claude-sonnet-5.5
OPENROUTER_REVIEW_MODEL=anthropic/claude-sonnet-5.5
OPENROUTER_WEB_QUERY_MODEL=anthropic/claude-haiku-5.5
OPENROUTER_LEADS_MODEL=anthropic/claude-haiku-5.5
OPENROUTER_REACH_MODEL=anthropic/claude-haiku-5.5
OPENROUTER_FINAL_MODEL=anthropic/claude-opus-5.5
ENV
docker compose --env-file .env up -d campaign-runner
```

Проверка: запустить в боте одну небольшую тестовую кампанию (бюджет по O10) и довести до отчёта; затем служебный запрос
LLM-ошибок (должно быть 0 строк `http_400`) и расход по моделям:

```bash
psqlc "select item as model, count(*) calls, round(sum(cost_usd),4) usd from campaign_costs where stage='llm' and kind='cost' and created_at > now() - interval '1 hour' group by 1 order by 3 desc"
```

Норма: в списке видны новые id; общая цена кампании на $0.2-0.4 выше прежней (MODELS.md). Один неверный id находится по
`item` в строке ошибки: откатывайте только его. Откат всех шести:

```bash
cat >> .env <<'ENV'
OPENROUTER_PLAN_MODEL=anthropic/claude-sonnet-4.5
OPENROUTER_REVIEW_MODEL=anthropic/claude-sonnet-4.5
OPENROUTER_WEB_QUERY_MODEL=openai/gpt-4o-mini
OPENROUTER_LEADS_MODEL=openai/gpt-4o-mini
OPENROUTER_REACH_MODEL=openai/gpt-4o-mini
OPENROUTER_FINAL_MODEL=anthropic/claude-sonnet-4.5
ENV
docker compose --env-file .env up -d campaign-runner
```

### (б) Apify-сначала и порталы (`ACQUISITION_MODE`, `APIFY_*_ENABLED`)

Нужен `APIFY_TOKEN`. Режим `apify`: сначала порталы, поисковик только если порталы дали меньше `APIFY_MIN_LISTINGS`
объявлений; браузерный социальный поиск выключен. Читает `campaign-runner`. Запуск каждого актора идёт с `maxTotalChargeUsd`
(`APIFY_*_MAX_CHARGE_USD`, по умолчанию $0.10) и пишется в `campaign_costs`.

Примечание к плану: отдельных `APIFY_FOTOCASA_PROVIDER` и `APIFY_YAENCONTRE_PROVIDER` нет. Переменная `*_PROVIDER` есть только у
Idealista (`APIFY_IDEALISTA_PROVIDER`: `axlymxp` по умолчанию или `igolaizola`); Fotocasa и Yaencontre включаются своим `*_ENABLED`.

**(б1) Fotocasa (подтверждён живым запуском).**

```bash
cat >> .env <<'ENV'
ACQUISITION_MODE=apify
APIFY_MIN_LISTINGS=5
APIFY_FOTOCASA_ENABLED=true
ENV
docker compose --env-file .env up -d campaign-runner
```

Проверка: тестовая кампания (например участки в Мадриде), затем:

```bash
psqlc "select name, state, error_code, jsonb_array_length(listings) as listings, updated_at from web_listing_source_runs order by updated_at desc limit 5"
psqlc "select provider, item, code, units, cost_usd from campaign_costs where provider is not null and stage='api' order by created_at desc limit 10"
```

Норма: `fotocasa`, `state=completed`, `listings > 0`, пустой `error_code`, цена порядка $0.001 за объявление. Откат:

```bash
printf '%s\n' 'ACQUISITION_MODE=legacy' 'APIFY_FOTOCASA_ENABLED=false' >> .env && docker compose --env-file .env up -d campaign-runner
```

**(б2) Yaencontre и Idealista (igolaizola). P2: только после живых сэмплов B5 и вашего «ок» (O3).**
Эти два актора проверены по схеме выдачи из Apify Store, а не живым запуском; Pisos, Habitaclia и Milanuncios тоже без сэмпла
(`docs/apify_portals.md`), их не включаем.

```bash
cat >> .env <<'ENV'
APIFY_YAENCONTRE_ENABLED=true
APIFY_IDEALISTA_ENABLED=true
APIFY_IDEALISTA_PROVIDER=igolaizola
ENV
docker compose --env-file .env up -d campaign-runner
```

Проверка: те же два запроса; для каждого портала `state=completed` и `listings > 0`, первую карточку сверить глазами со ссылкой.
Откат:

```bash
printf '%s\n' 'APIFY_YAENCONTRE_ENABLED=false' 'APIFY_IDEALISTA_ENABLED=false' 'APIFY_IDEALISTA_PROVIDER=axlymxp' >> .env && docker compose --env-file .env up -d campaign-runner
```

### (в) Facebook-группы через Apify (⛔ O1)

Только публичные группы; закрытые остаются у браузера (`APIFY_FACEBOOK_BROWSER_FALLBACK=true`). Без `APIFY_FACEBOOK_ACTOR_BUILD`
сервисы Facebook **отказываются стартовать**. Читают `facebook-runner` (и `facebook-collector`, если профиль `collector` включён).
Сначала один осторожный прогон по `docs/apify_adapter.md` («Первый ручной прогон»), потом `all`.

Первая чистка и крон описаны в разделе 6; перед первым включением прогнать:
`docker compose --env-file .env run --rm facebook-runner python -m bot.facebook_collector.apify.retention --dry-run`.

**(в1) Один прогон, `scope=campaign`.**

```bash
cat >> .env <<'ENV'
FEATURE_APIFY_FACEBOOK=true
APIFY_FACEBOOK_ACTOR_BUILD=<номер билда из Apify Console>
APIFY_FACEBOOK_SCOPE=campaign
APIFY_FACEBOOK_MAX_POSTS_PER_RUN=50
APIFY_FACEBOOK_MAX_USD_PER_RUN=0.5
CAMPAIGN_BUDGET_USD=1
ENV
docker compose --env-file .env up -d facebook-runner
docker compose --env-file .env logs --tail=20 facebook-runner      # не должно быть отказа про build / token
```

Создать кампанию с одной открытой группой и перевести её на Apify (SQL из `docs/apify_adapter.md`):
`psqlc "update campaigns set acquisition_provider='apify' where id='<campaign-uuid>'"`. Проверка:

```bash
psqlc "select run_id, status, items_imported, billed_usd, import_checkpoint from apify_runs order by launched_at desc limit 3"
psqlc "select stage, kind, code, sum(units), sum(cost_usd) from campaign_costs where campaign_id='<campaign-uuid>' group by 1,2,3"
```

Норма: `status=SUCCEEDED`, `items_imported > 0`, `billed_usd` совпадает с Apify Billing. После прогона вернуть лимиты:
`CAMPAIGN_BUDGET_USD` на значение из O10 и убрать две строки `MAX_*` (пустая строка не нужна, пишем умолчания:
`APIFY_FACEBOOK_MAX_POSTS_PER_RUN=500`, `APIFY_FACEBOOK_MAX_USD_PER_RUN=2`).

**(в2) Все кампании, `scope=all`.**

```bash
cat >> .env <<'ENV'
APIFY_FACEBOOK_SCOPE=all
APIFY_FACEBOOK_MAX_POSTS_PER_RUN=500
APIFY_FACEBOOK_MAX_USD_PER_RUN=2
CAMPAIGN_BUDGET_USD=<значение O10>
ENV
docker compose --env-file .env up -d facebook-runner
```

Откат (полный, оба шага):

```bash
printf '%s\n' 'FEATURE_APIFY_FACEBOOK=false' 'APIFY_FACEBOOK_SCOPE=campaign' >> .env && docker compose --env-file .env up -d facebook-runner
```

(если был `collector`: добавьте `facebook-collector` в `up -d`). Уже созданные записи `apify_runs` остаются как аудит.

### (г) Комментарии под постом и лиды через Apify (⛔ O1)

`APIFY_FACEBOOK_COMMENTS_ENABLED` читают `telegram` (иначе ссылка на пост остаётся обычным текстом задачи) и `campaign-runner`
(без `APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD` он не стартует). Цена: $0.0025 за комментарий + $0.001 за старт.

**(г1) Ссылка на пост.**

```bash
cat >> .env <<'ENV'
APIFY_FACEBOOK_COMMENTS_ENABLED=true
APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD=<номер билда комментариев>
ENV
docker compose --env-file .env up -d telegram campaign-runner
docker compose --env-file .env logs --tail=20 campaign-runner      # нет отказа про build
```

Проверка: отправить боту ссылку на пост из O7 (30 и более комментариев):

```bash
psqlc "select * from campaign_post_reads order by 1 desc limit 3"
psqlc "select count(*) from investor_leads where campaign_id='<campaign-uuid>'"
psqlc "select stage, kind, code, sum(units), sum(cost_usd) from campaign_costs where campaign_id='<campaign-uuid>' group by 1,2,3"
```

Норма: строка чтения с `run_id`; стоимость около $0.0025 на комментарий. Откат:

```bash
echo 'APIFY_FACEBOOK_COMMENTS_ENABLED=false' >> .env && docker compose --env-file .env up -d telegram campaign-runner
```

**(г2) Лиды под объектами кампании через Apify (`LEADS_PROVIDER`).** Дневной лимит `APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY=40` (умолчание).

```bash
cat >> .env <<'ENV'
LEADS_PROVIDER=apify
ENV
docker compose --env-file .env up -d campaign-runner
```

Проверка: после кампании с отправленными карточками из Facebook `psqlc "select count(*) from campaign_comment_reads where run_id is not null"`
больше 0, расходы в `campaign_costs`. Если `COMMENTS_ENABLED` не включён, бот пишет предупреждение и читает браузером. Откат:

```bash
echo 'LEADS_PROVIDER=browser' >> .env && docker compose --env-file .env up -d campaign-runner
```

### (д) Приглашения в закрытые группы, отчёт по инвесторам, режим «оба»

```bash
cat >> .env <<'ENV'
CAMPAIGN_JOIN_NOTICES=true
CAMPAIGN_INVESTOR_REPORT=true
ENV
docker compose --env-file .env up -d telegram campaign-runner
```

* `CAMPAIGN_JOIN_NOTICES` (telegram и campaign-runner): для закрытой группы одно сообщение с кнопкой «Вступил ✅», потом чтение браузером.
  Вступает человек вручную; бот в группу не вступает.
* `CAMPAIGN_INVESTOR_REPORT` (campaign-runner): в итоговом отчёте поиска investors/both раздел «💼 Инвесторы» и деньги по ветвям.

Проверка: поиск инвесторов (режим investors) до отчёта: в отчёте есть раздел «💼 Инвесторы»; закрытая группа даёт сообщение с «Вступил ✅».
Откат:

```bash
printf '%s\n' 'CAMPAIGN_JOIN_NOTICES=false' 'CAMPAIGN_INVESTOR_REPORT=false' >> .env && docker compose --env-file .env up -d telegram campaign-runner
```

`INTAKE_BOTH_MODE`: **появится в задаче C2, в этой ветке переменной нет** (ни в `.env.example`, ни в `docker-compose.yml`). Не добавлять
в `.env` вручную. После мержа C2 строки включения и отката будут в его описании; шаблон: `cat >> .env`, `up -d telegram`,
откат дописать прежнее значение. Режим `both` выбирается пользователем в меню; флаг только скрывает кнопку.

### (е) Архитектор задач на Claude API (⛔ O4)

Только после (а1). Сверьте id `claude-sonnet-5-5` и цену с документацией Anthropic. Читает `telegram`. При ошибке Claude
отвечает OpenRouter, затем встроенные правила. Без ключа бот **не стартует**.

```bash
# ключ вписать руками, не вставляя в чат и не печатая:
cat >> .env <<'ENV'
ANTHROPIC_API_KEY=<ключ из Anthropic Console>
ARCHITECT_PROVIDER=anthropic
ENV
docker compose --env-file .env up -d telegram
docker compose --env-file .env logs --tail=20 telegram | grep -iE "error|traceback" || echo ok
```

Проверка: новая задача в боте, интервью идёт; в логе нет `telegram.intake.interview_fallback` (он означает, что Claude не ответил и
сработал запасной путь); `grep -c '^ANTHROPIC_API_KEY=.' .env` даёт `1`. Расход смотреть в Anthropic Console (лимит трат задан). Откат:

```bash
echo 'ARCHITECT_PROVIDER=openrouter' >> .env && docker compose --env-file .env up -d telegram
```

Ключ можно оставить в `.env`; убрать совсем: `sed -i '/^ANTHROPIC_API_KEY=/d' .env` и `up -d telegram`.

### (ж) Watchdog: алерты владельцу (⛔ O8, O11)

Подробности и риски сокета: `docs/v2/OPS.md` (сокет `:ro` даёт процессу полный доступ к Docker API, это решение O11).
Если в `.env` уже есть `COMPOSE_PROFILES`, **добавьте** `ops` через запятую в существующую строку, а не дописывайте вторую.

```bash
grep -E '^COMPOSE_PROFILES' .env || echo "COMPOSE_PROFILES не задан"
stat -c %g /var/run/docker.sock                 # число = WATCHDOG_DOCKER_GID
cat >> .env <<'ENV'
COMPOSE_PROFILES=ops
WATCHDOG_ENABLED=true
OWNER_ALERT_CHAT_ID=<ваш chat id>
WATCHDOG_DOCKER_SOCKET=/var/run/docker.sock
WATCHDOG_DOCKER_GID=<число из stat>
ENV
docker compose --env-file .env up -d --build watchdog
docker compose --env-file .env logs --tail=20 watchdog       # "watchdog started"
docker compose --env-file .env ps watchdog                   # healthy
```

Остальное по умолчанию: `WATCHDOG_POLL_SECONDS=60`, `WATCHDOG_ALERT_COOLDOWN_MINUTES=30`, `WATCHDOG_DAILY_SUMMARY_HOUR=9` (UTC).
Без контроля контейнеров оставьте `WATCHDOG_DOCKER_SOCKET` пустым.

Тест алерта (нужно `stop`, а не `kill`: при `kill` политика `unless-stopped` сразу поднимает контейнер, и состояние `exited` не застать):

```bash
docker compose --env-file .env stop analysis-worker
# ждать сообщение в Telegram "Сервис analysis-worker: exited" не дольше 2 минут
docker compose --env-file .env up -d analysis-worker
docker compose --env-file .env ps analysis-worker            # healthy
```

Норма: сообщение пришло за 2 минуты или меньше. Не пришло: `logs --tail=50 watchdog`, проверить `OWNER_ALERT_CHAT_ID` и что вы
писали боту `/start` хотя бы раз. Дневная сводка придёт в 09:00 UTC. Откат (мягкий, контейнер спит; полный см. раздел 5):

```bash
echo 'WATCHDOG_ENABLED=false' >> .env && docker compose --env-file .env up -d watchdog
```

## 4. Проверки после всего

Тестовая кампания G1 из `docs/v2/GOLDEN_SET.md` (id: `psqlc "select id, left(source_text,60), state from campaigns order by created_at desc limit 5"`).

```bash
cd /opt/real-estate-bot
./scripts/eval_campaign.sh <campaign-uuid>        # только чтение: воронка, источники, расходы, лиды, длительность, модели
docker compose --env-file .env ps --format 'table {{.Service}}\t{{.Status}}'
docker compose --env-file .env logs --since=30m | grep -iE "traceback|critical" || echo "traceback нет"
```

Деньги и источники:

```bash
psqlc "select stage, kind, coalesce(code,'-') code, count(*) n, round(sum(cost_usd),4) usd from campaign_costs where campaign_id='<campaign-uuid>' group by 1,2,3 order by usd desc"
psqlc "select run_id, status, items_imported, billed_usd, launched_at from apify_runs order by launched_at desc limit 5"
psqlc "select name, state, coalesce(error_code,'-') error_code, jsonb_array_length(listings) listings from web_listing_source_runs where campaign_id='<campaign-uuid>'"
```

Норма: итого на кампанию в пределах `CAMPAIGN_BUDGET_USD`; `billed_usd` в `apify_runs` совпадает с Apify Billing; порталы `completed`;
`kind='error'` нет или единичные с понятным кодом; хосты ссылок преимущественно из `SPAIN_PORTALS` (при `apify_enough`).
Результат оценки сверить с `docs/v2/GOLDEN_SET.md`.

## 5. Откат

### 5.1 Флаги: что дописать, что перезапустить

Откат = дописать строку из столбца 3 в конец `.env` и выполнить `docker compose --env-file .env up -d <сервисы>`.
Если строка этого ключа в старом `.env` была другой (свои значения), ориентируйтесь на `.env.bak.*` из раздела 1.

| Часть | Флаг -> откат (строка `.env`) | Сервисы для `up -d` |
|---|---|---|
| Интервью | `OPENROUTER_INTERVIEW_MODEL=anthropic/claude-sonnet-4.5` | telegram |
| Анализ | `OPENROUTER_ANALYSIS_MODEL=anthropic/claude-sonnet-4.5` | analysis-worker (+ analysis-pipeline при профиле `analysis`) |
| План / отбор / отчёт | `OPENROUTER_PLAN_MODEL=`, `OPENROUTER_REVIEW_MODEL=`, `OPENROUTER_FINAL_MODEL=` равны `anthropic/claude-sonnet-4.5` | campaign-runner |
| Запросы web / лиды / reach | `OPENROUTER_WEB_QUERY_MODEL=`, `OPENROUTER_LEADS_MODEL=`, `OPENROUTER_REACH_MODEL=` равны `openai/gpt-4o-mini` | campaign-runner |
| Apify-сначала | `ACQUISITION_MODE=legacy` | campaign-runner |
| Порталы | `APIFY_FOTOCASA_ENABLED=false`, `APIFY_YAENCONTRE_ENABLED=false`, `APIFY_IDEALISTA_ENABLED=false`, `APIFY_IDEALISTA_PROVIDER=axlymxp` | campaign-runner |
| FB-группы через Apify | `FEATURE_APIFY_FACEBOOK=false` (и `APIFY_FACEBOOK_SCOPE=campaign`) | facebook-runner (+ facebook-collector при профиле `collector`) |
| Комментарии под постом | `APIFY_FACEBOOK_COMMENTS_ENABLED=false` | telegram, campaign-runner |
| Лиды через Apify | `LEADS_PROVIDER=browser` | campaign-runner |
| Приглашения в закрытые группы | `CAMPAIGN_JOIN_NOTICES=false` | telegram, campaign-runner |
| Отчёт по инвесторам | `CAMPAIGN_INVESTOR_REPORT=false` | campaign-runner |
| Архитектор на Claude | `ARCHITECT_PROVIDER=openrouter` | telegram |
| Watchdog (мягко) | `WATCHDOG_ENABLED=false` | watchdog |
| Watchdog (полностью) | убрать `ops` из `COMPOSE_PROFILES` в `.env`, затем `docker compose --env-file .env --profile ops rm -sf watchdog` | watchdog |
| `INTAKE_BOTH_MODE` | появится в C2 | telegram |

Всё сразу к состоянию до флагов: `cp .env.bak.<метка из раздела 1> .env`, затем
`docker compose --env-file .env up -d` (нужные профили берутся из `COMPOSE_PROFILES`; если сервис `watchdog` уже запущен, а профиль
убран, остановите его командой из таблицы).

### 5.2 Откат кода

Миграции только добавляющие, поэтому старый код работает на новой схеме.

```bash
cd /opt/real-estate-bot
git status --short --untracked-files=no                    # пусто
git checkout <предыдущий-коммит из раздела 2>
docker compose --env-file .env up -d --build
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:8080/healthz && echo healthz ok
```

Вернуться на `main`: `BRANCH=main ./scripts/update.sh`. Пока вы на отдельном коммите (detached HEAD), `update.sh` без `BRANCH=main` не годится.
Восстановление БД из бэкапа (`/root/backups/bot-*.sql.gz`) только в крайнем случае, с остановкой сервисов; сначала посоветуйтесь.

### 5.3 Откат миграций (`scripts/rollback/*`)

Обычно **не нужен**: миграции добавляют колонки и таблицу. Нужен, только если требуется вернуть схему на 046. Порядок строго
обратный: 049, затем 048, затем 047. Сначала бэкап (раздел 1) и откат кода (5.2) на коммит, где этих колонок/таблиц ещё нет.
Каждый скрипт сам **отказывается** выполняться, если в данных что-то есть, и ничего не теряет:

| Скрипт | Откажет, если |
|---|---|
| `scripts/rollback/049_campaign_comment_reads_run_id_down.sql` | в `campaign_comment_reads.run_id` есть значения (запись оплаченного запуска Apify) |
| `scripts/rollback/048_campaign_post_reads_down.sql` | в `campaign_post_reads` есть строки |
| `scripts/rollback/047_campaign_group_join_down.sql` | у групп заполнены `join_notice_at` / `join_confirmed_at` |

После каждого скрипта удалить его строку из `schema_migrations`, иначе миграция при следующем `update.sh` не применится заново:

```bash
cd /opt/real-estate-bot
for n in 049_campaign_comment_reads_run_id 048_campaign_post_reads 047_campaign_group_join; do
  docker compose --env-file .env exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "scripts/rollback/${n}_down.sql" \
  && psqlc "delete from public.schema_migrations where filename = '${n}.sql'" || break
done
```

Цикл останавливается на первом отказе (`|| break`); тогда данные в таблице реальные, откат этой миграции не делать
(оставить схему, старому коду она не мешает). Скрипты откатов в `update.sh` и `apply_migrations.sh` не вызываются.

Умолчания (если строки в `.env` нет): модели как в столбце «Было» `docs/v2/MODELS.md`; `ACQUISITION_MODE=legacy`; все
`*_ENABLED=false`; `APIFY_IDEALISTA_PROVIDER=axlymxp`; `APIFY_FACEBOOK_SCOPE=campaign`; `LEADS_PROVIDER=browser`;
`ARCHITECT_PROVIDER=openrouter`; `WATCHDOG_ENABLED=false`.

## 6. Cron и перезагрузка VPS

### Cron (`crontab -e`, root; из `docs/v2/OPS.md`)

```cron
0 3 * * *   cd /opt/real-estate-bot && ./scripts/backup.sh >> /var/log/re-backup.log 2>&1 || echo "backup failed" >> /var/log/re-backup.log
30 4 * * 0  cd /opt/real-estate-bot && ./scripts/restore_check.sh >> /var/log/re-backup.log 2>&1
15 4 * * *  cd /opt/real-estate-bot && docker compose --env-file .env run --rm facebook-runner python -m bot.facebook_collector.apify.retention >> /var/log/re-backup.log 2>&1
```

Третья строка нужна только после блока (в). Раз в неделю: `tail -20 /var/log/re-backup.log` (должно быть `OK:` и `OK, campaigns=`).
Отключить: закомментировать строку в `crontab -e`.

### Перезагрузка (⛔ O9, только в согласованное окно)

```bash
cd /opt/real-estate-bot
set -o pipefail; ./scripts/backup.sh                    # при ошибке дальше не идти
docker compose --env-file .env down                     # без -v: тома остаются
reboot
# после входа по SSH:
cd /opt/real-estate-bot && docker compose --env-file .env up -d
sleep 60
docker compose --env-file .env ps                       # все Up, healthy
curl -fsS http://127.0.0.1:8080/healthz && echo healthz ok
docker compose --env-file .env logs --since=5m | grep -iE "error|traceback|critical" || echo "ошибок нет"
```

После `down` сервисы сами не стартуют, поэтому `up -d` обязателен. Алерт watchdog о недоступности сразу после перезагрузки допустим;
если сервис остался `unhealthy` через 3 минуты, `docker compose --env-file .env logs --tail=50 <сервис>`.
