# P0: включение v2 на VPS (чек-лист владельца)

Закрывает v2 A4, D2, D1 (в v3: P0.2, P0.3, P0.4). Решение D21: сначала v2 на VPS, потом v3.
Это сжатый `docs/v2/DEPLOY.md` §0–§4; подробности по ссылкам `DEPLOY.md:NNN`. Всё на VPS, под root, в `/opt/real-estate-bot`.

Правила (`docs/v2/DEPLOY.md:10-14`): не совпало с «Норма» — стоп и «Откат» блока. Откат = прежнее значение в конец `.env`
и `up -d` (Compose берёт **последнюю** строку ключа). Секреты не печатать и не копировать в чат; `<...>` вписываете сами.

Решения, учтённые здесь (`docs/v2/CLIENT_DECISIONS.md:71-88`): O1 принято (1a–1e) → блоки (г) и A4 разрешены,
`CARD_EXTRA_FIELDS=true`, `CAMPAIGN_JOIN_NOTICES=true`; O4 — только OpenRouter → блок (е) **пропускается**;
O2 — `APIFY_FACEBOOK_SCOPE=all` без отдельного решения не включать → (в2) пропускается; O10 — `CAMPAIGN_BUDGET_USD=5`.

## 0. Перед началом (только чтение, 10 мин) — `docs/v2/DEPLOY.md:36-73`

```bash
cd /opt/real-estate-bot
df -h / /var/lib/docker | tail -2 ; docker compose --env-file .env version --short
git status --short --untracked-files=no ; git rev-parse --short HEAD     # пусто; хэш записать (откат кода)
docker compose --env-file .env config -q && echo "compose config ok"
for k in POSTGRES_PASSWORD REDIS_PASSWORD DATABASE_URL TELEGRAM_TOKEN OPENROUTER_API_KEY APIFY_TOKEN; do
  printf '%-20s %s\n' "$k" "$(grep -c "^$k=." .env)"; done
# снимок флагов до update.sh: файл вне репозитория, по нему делаются все откаты
grep -nE '^(ACQUISITION_MODE|APIFY_[A-Z_]+_ENABLED|FEATURE_APIFY_FACEBOOK|LEADS_PROVIDER|CAMPAIGN_BUDGET_USD|APIFY_FACEBOOK_SCOPE|COMPOSE_PROFILES|CAMPAIGN_JOIN_NOTICES|CAMPAIGN_INVESTOR_REPORT|INTAKE_BOTH_MODE|CARD_EXTRA_FIELDS|ARCHITECT_PROVIDER|WATCHDOG_ENABLED|OPENROUTER_[A-Z_]+_MODEL)=' .env > /root/p0-flags-before.txt
cat /root/p0-flags-before.txt; [ -s /root/p0-flags-before.txt ] || echo "ни одной строки: умолчания (всё выключено)"
stat -c %g /var/run/docker.sock                                           # GID для блока (ж)
psqlc() { docker compose --env-file .env exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' sh "$1"; }
llmerr() { psqlc "select stage, code, item, count(*) from campaign_costs where kind='error' and created_at > now() - interval '30 minutes' group by 1,2,3 order by 4 desc"; }
```

Норма: свободно ≥ 5 ГБ; дерево чистое; все шесть ключей `1`; версия Compose v2 (запишите её — `HANDOFF.md` §4 п. 16).
Снимок `/root/p0-flags-before.txt` (`docs/v2/DEPLOY.md:56`): флаги выключены (`ACQUISITION_MODE=legacy`, `*_ENABLED=false`,
`FEATURE_APIFY_FACEBOOK=false`, `LEADS_PROVIDER=browser`, `APIFY_FACEBOOK_SCOPE=campaign`, `CAMPAIGN_BUDGET_USD=5`) или строк нет.
Что-то включено (на проде было `FEATURE_APIFY_FACEBOOK=true`, `docs/v2/HANDOFF.md:225-231`) — **стоп**, до `update.sh`
написать разработчику. Откаты ниже написаны для умолчаний: если в снимке у ключа другое значение, возвращайте значение из снимка.
Записать заранее: билды акторов `apify/facebook-groups-scraper` и `apify/facebook-comments-scraper` (вкладка Builds,
вид `0.1.23`, не `latest`; `docs/v2/DEPLOY.md:25-26`), ссылки на посты O7 для G6/G8. `psqlc`/`llmerr` задавать в каждой новой SSH-сессии.

## 1. Бэкап — `docs/v2/DEPLOY.md:75-92`

```bash
cd /opt/real-estate-bot
set -o pipefail
chmod 700 /root/backups 2>/dev/null || true
./scripts/backup.sh && ls -lh /root/backups | tail -3 && ./scripts/restore_check.sh
cp .env .env.bak.$(date +%F-%H%M) && ls -l .env.bak.* | tail -1
```

Норма: `OK: /root/backups/bot-…sql.gz (N bytes)`, N заметно больше 10 КБ (≤ 10 КБ скрипт удаляет и выходит с 1),
затем `OK, campaigns=N`. Ошибка — стоп. Откат: не нужен (ничего не меняет).

## 2. Обновление кода и миграции (D2) — `docs/v2/DEPLOY.md:94-138`

```bash
cd /opt/real-estate-bot
BRANCH=main ./scripts/update.sh 2>&1 | grep -vE '^ =>|NOTICE|Already applied'; echo "update.sh exit=${PIPESTATUS[0]}"
psqlc "select filename from public.schema_migrations where filename >= '046' order by filename"
sleep 70
docker compose --env-file .env ps --format 'table {{.Service}}\t{{.Status}}'
docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}нет{{end}}' $(docker compose --env-file .env ps -q)
docker compose --env-file .env logs --since=5m | grep -iE "traceback|critical" || echo "ошибок нет"
curl -fsS http://127.0.0.1:8080/healthz && echo " healthz ok"
```

Норма (`docs/v2/DEPLOY.md:107-128`): `update.sh exit=0`, `Migrations are current.`, `Caddy healthz: OK`; миграции
046–050; у каждого сервиса `restart=unless-stopped` и `health=healthy` (нет `unhealthy`, `Restarting`); `ошибок нет`;
`healthz ok`. У сервисов профилей `collector`, `agent-reach`, `scrapling`, `analysis` нет healthcheck и restart. Откат кода (`docs/v2/DEPLOY.md:608-622`):

```bash
git checkout <хэш из п. 0> && docker compose --env-file .env up -d --build && curl -fsS http://127.0.0.1:8080/healthz && echo ok
```

## 3. Флаги по блокам — `docs/v2/DEPLOY.md:140-552`

После каждого блока: проверка, потом следующий. `llmerr` — LLM-ошибки за 30 минут (`docs/v2/DEPLOY.md:145-147`).

### (а) Модели 5.5 через OpenRouter — `docs/v2/DEPLOY.md:149-248`

До включения сверить id на openrouter.ai/models (`docs/v2/DEPLOY.md:32-34`). По одному подблоку.

```bash
echo 'OPENROUTER_INTERVIEW_MODEL=anthropic/claude-sonnet-5.5' >> .env && docker compose --env-file .env up -d telegram
echo 'OPENROUTER_ANALYSIS_MODEL=anthropic/claude-sonnet-5.5' >> .env && docker compose --env-file .env up -d analysis-worker
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

При профиле `analysis` после (а2) ещё `docker compose --env-file .env up -d analysis-pipeline` (и в откате (а2)).

Норма (`docs/v2/DEPLOY.md:175-176`, `:192-193`, `:213-236`): бот задаёт уточняющий вопрос, в логе `telegram` нет
`interview_unreadable`; в `analysis-worker` нет `request_refused|model_unavailable`; после одной небольшой кампании до
отчёта — 0 строк `http_400` в `llmerr`, новые id видны в `campaign_costs.item`. Откат (по одной ступени, `:179`, `:196`, `:238-248`):

```bash
echo 'OPENROUTER_INTERVIEW_MODEL=anthropic/claude-sonnet-4.5' >> .env && docker compose --env-file .env up -d telegram
echo 'OPENROUTER_ANALYSIS_MODEL=anthropic/claude-sonnet-4.5' >> .env && docker compose --env-file .env up -d analysis-worker
printf '%s\n' OPENROUTER_PLAN_MODEL=anthropic/claude-sonnet-4.5 OPENROUTER_REVIEW_MODEL=anthropic/claude-sonnet-4.5 OPENROUTER_WEB_QUERY_MODEL=openai/gpt-4o-mini OPENROUTER_LEADS_MODEL=openai/gpt-4o-mini OPENROUTER_REACH_MODEL=openai/gpt-4o-mini OPENROUTER_FINAL_MODEL=anthropic/claude-sonnet-4.5 >> .env && docker compose --env-file .env up -d campaign-runner
```

### (б) Apify-сначала и порталы — `docs/v2/DEPLOY.md:250-317`

```bash
printf '%s\n' ACQUISITION_MODE=apify APIFY_MIN_LISTINGS=5 APIFY_FOTOCASA_ENABLED=true >> .env && docker compose --env-file .env up -d campaign-runner
```

Норма (`docs/v2/DEPLOY.md:270-277`): тестовая кампания, затем
`psqlc "select name, state, error_code, jsonb_array_length(listings) from web_listing_source_runs order by updated_at desc limit 5"`
→ `fotocasa`, `completed`, `listings > 0`, `error_code` пуст. (б2) Yaencontre + Idealista и (б3) Pisos/Habitaclia/Milanuncios —
только с вашим «ок» по O3, по одному порталу, норма как в (б1) (`docs/v2/DEPLOY.md:283-317`); без «ок» пропустить.
Откат (`:280`, `:300`, `:316`; первые два значения — (б1)):

```bash
printf '%s\n' ACQUISITION_MODE=legacy APIFY_FOTOCASA_ENABLED=false APIFY_YAENCONTRE_ENABLED=false APIFY_IDEALISTA_ENABLED=false APIFY_IDEALISTA_PROVIDER=axlymxp APIFY_PISOS_ENABLED=false APIFY_HABITACLIA_ENABLED=false APIFY_MILANUNCIOS_ENABLED=false >> .env && docker compose --env-file .env up -d campaign-runner
```

### (в) Facebook-группы через Apify — перенесён в раздел 4 (A4)

### (г) Комментарии под постом и лиды через Apify — `docs/v2/DEPLOY.md:382-429`

```bash
printf '%s\n' APIFY_FACEBOOK_COMMENTS_ENABLED=true 'APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD=<билд комментариев>' LEADS_PROVIDER=apify APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY=0 >> .env
docker compose --env-file .env up -d telegram campaign-runner
docker compose --env-file .env logs --tail=20 campaign-runner | grep -iE "build|traceback" || echo ok
```

Норма (`docs/v2/DEPLOY.md:398-406`, `:424-425`): ссылка на пост O7 → строка в `campaign_post_reads` с `run_id`,
около $0.0025 за комментарий; `campaign_comment_reads where run_id is not null` > 0. Откат (`:409`, `:428`):

```bash
printf '%s\n' APIFY_FACEBOOK_COMMENTS_ENABLED=false LEADS_PROVIDER=browser APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY=40 >> .env && docker compose --env-file .env up -d telegram campaign-runner
```

### (д) Закрытые группы, отчёт по инвесторам, режим «оба» — `docs/v2/DEPLOY.md:431-469`

`CAMPAIGN_JOIN_NOTICES=true` — решение O1-1d. `INTAKE_BOTH_MODE` только вместе с `CAMPAIGN_INVESTOR_REPORT` (`:452-454`).

```bash
printf '%s\n' CAMPAIGN_JOIN_NOTICES=true CAMPAIGN_INVESTOR_REPORT=true INTAKE_BOTH_MODE=true >> .env
docker compose --env-file .env up -d telegram campaign-runner
docker compose --env-file .env logs --tail=20 telegram | grep -iE "error|traceback" || echo ok
```

Норма (`docs/v2/DEPLOY.md:445`, `:464`): в меню кнопка «🏡💼 Недвижимость + инвесторы»; отчёт investors содержит
«💼 Инвесторы»; закрытая группа даёт сообщение с «Вступил ✅». Откат (`:449`, `:468`):

```bash
printf '%s\n' CAMPAIGN_JOIN_NOTICES=false CAMPAIGN_INVESTOR_REPORT=false INTAKE_BOTH_MODE=false >> .env && docker compose --env-file .env up -d telegram campaign-runner
```

### (е) Архитектор на Claude API — ПРОПУСТИТЬ (O4: только OpenRouter)

`docs/v2/DEPLOY.md:471-493` не выполнять. Проверка, что остаётся OpenRouter:
`grep -E '^ARCHITECT_PROVIDER=' .env || echo "умолчание openrouter"` → пусто или `openrouter`.

### (ж) Watchdog: алерты владельцу — `docs/v2/DEPLOY.md:495-532`

Риск `HANDOFF.md` §4 п. 16: если `COMPOSE_PROFILES` уже есть, `ops` дописывается в ту же строку (`collector,ops`), второй строки нет.

```bash
grep -q '^COMPOSE_PROFILES=' .env && sed -i '/^COMPOSE_PROFILES=/{/ops/!s/$/,ops/}' .env || echo COMPOSE_PROFILES=ops >> .env
grep -E '^COMPOSE_PROFILES=' .env                       # одна строка, в ней ops
printf '%s\n' WATCHDOG_ENABLED=true WATCHDOG_DOCKER_SOCKET=/var/run/docker.sock 'WATCHDOG_DOCKER_GID=<GID из stat>' >> .env
docker compose --env-file .env up -d --build watchdog
docker compose --env-file .env ps -q watchdog | grep -q . || docker compose --env-file .env --profile ops up -d watchdog
docker compose --env-file .env ps watchdog
docker compose --env-file .env logs --tail=30 watchdog | grep -iE "started|permission|denied|docker" 
docker compose --env-file .env stop analysis-worker    # ждать алерт «Сервис analysis-worker: exited» ≤ 2 мин
docker compose --env-file .env up -d analysis-worker && docker compose --env-file .env ps analysis-worker
```

Норма (`docs/v2/DEPLOY.md:510-526`): `watchdog started`, `healthy`; алерт пришёл не позже 2 минут; `analysis-worker`
снова `healthy`. Именно `stop`, не `kill` (`:517`). Проверка п. 16: после следующего `update.sh` watchdog есть в `ps` без `--profile`.

Риск `HANDOFF.md` §4 п. 15 (`cap_drop: [ALL]` и сокет): в логе `permission denied` или алерт «docker недоступен» →
сверить `stat -c %g /var/run/docker.sock` с `WATCHDOG_DOCKER_GID`; не помогло — оставить алерты без контроля контейнеров
(`echo 'WATCHDOG_DOCKER_SOCKET=' >> .env` и `up -d watchdog`) и написать мне: убрать `cap_drop` можно только отдельным
коммитом с вашим «ок». Откат (`:531`, полный — `:600`):

```bash
echo 'WATCHDOG_ENABLED=false' >> .env && docker compose --env-file .env up -d watchdog
```

### (з) Карточка: площадь, телефон, агентство — `docs/v2/DEPLOY.md:534-552`

O1-1b: телефон для всех объявлений, включая частников.

```bash
echo 'CARD_EXTRA_FIELDS=true' >> .env && docker compose --env-file .env up -d analysis-worker campaign-runner
```

При профиле `analysis` ещё `docker compose --env-file .env up -d analysis-pipeline` (и в откате).

Норма (`docs/v2/DEPLOY.md:547-548`): в новой карточке «Площадь» (участок отдельно), «Телефон», «Агентство», если портал
их отдал; `logs --since=10m analysis-worker campaign-runner | grep Traceback` пусто. Откат (`:551`):

```bash
echo 'CARD_EXTRA_FIELDS=false' >> .env && docker compose --env-file .env up -d analysis-worker campaign-runner
```

## 4. A4 (P0.2): одна открытая группа FB через Apify — `docs/v2/DEPLOY.md:319-360`

**Потолок A4: $0.30 на запуск актора групп, ≤ $0.6 на всю кампанию A4.** После (б) и (г) в кампании работают ещё
порталы, LLM и Apify-комментарии под карточками (до $1 на пост), поэтому на время A4 `LEADS_PROVIDER=browser` и
`CAMPAIGN_BUDGET_USD=0.5`. Бюджет проверяется до вызова: сверху возможен ещё один вызов. ≤ 50 постов.
`CAMPAIGN_BUDGET_USD` каждый сервис читает сам, поэтому перезапускаются все четыре: `watchdog` — только если (ж) включён.
Если в `COMPOSE_PROFILES` есть `collector` или `analysis`, добавляйте `facebook-collector` / `analysis-pipeline` во все
`up -d` этого раздела и раздела 6 (они тоже читают `CAMPAIGN_BUDGET_USD`).

Риск `HANDOFF.md` §4 п. 17: `facebook-runner` должен быть в сети `egress`, иначе `apify_launch_unknown`. Проверка до включения:

```bash
docker inspect "$(docker compose --env-file .env ps -q facebook-runner)" --format '{{json .NetworkSettings.Networks}}' | grep -o '"[a-z_-]*_\(backend\|egress\)"'
docker compose --env-file .env run --rm facebook-runner python -m bot.facebook_collector.apify.retention --dry-run
```

Норма: обе строки, `…backend` и `…egress`; нет `egress` — стоп (compose не обновился, вернуться к п. 2).

```bash
printf '%s\n' FEATURE_APIFY_FACEBOOK=true 'APIFY_FACEBOOK_ACTOR_BUILD=<билд групп>' APIFY_FACEBOOK_SCOPE=campaign APIFY_FACEBOOK_MAX_POSTS_PER_RUN=50 APIFY_FACEBOOK_MAX_USD_PER_RUN=0.3 APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN=0.3 LEADS_PROVIDER=browser CAMPAIGN_BUDGET_USD=0.5 >> .env
docker compose --env-file .env up -d facebook-runner campaign-runner analysis-worker watchdog   # watchdog — только если (ж) включён
docker compose --env-file .env logs --tail=20 facebook-runner | grep -iE "build|token|traceback" || echo ok
```

Создать в боте кампанию с одной открытой группой, затем (`docs/v2/DEPLOY.md:350-356`):

```bash
psqlc "update campaigns set acquisition_provider='apify' where id='<campaign-uuid>'"
psqlc "select run_id, status, items_imported, billed_usd from apify_runs order by launched_at desc limit 1"
psqlc "select stage, kind, code, sum(units), sum(cost_usd) from campaign_costs where campaign_id='<campaign-uuid>' group by 1,2,3"
psqlc "select round(sum(cost_usd),4) usd from campaign_costs where campaign_id='<campaign-uuid>' and kind='cost'"
```

Норма: `status=SUCCEEDED` (в БД заглавными), `items_imported > 0`, `billed_usd` ≤ 0.30 и совпадает с Apify Billing;
итог `campaign_costs` по кампании A4 ≤ $0.6. После A4 вернуть значения из снимка п. 0 и (г) (`docs/v2/DEPLOY.md:358-360`),
`scope=all` не включать (O2):

```bash
printf '%s\n' APIFY_FACEBOOK_MAX_POSTS_PER_RUN=500 APIFY_FACEBOOK_MAX_USD_PER_RUN=2 APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN=10 LEADS_PROVIDER=apify CAMPAIGN_BUDGET_USD=5 >> .env && docker compose --env-file .env up -d facebook-runner campaign-runner analysis-worker watchdog   # watchdog — если (ж) включён
```

Откат A4 (`docs/v2/DEPLOY.md:377`; `LEADS_PROVIDER=apify` — если (г) оставлен, иначе `browser`). Ретеншн-крон (`:663`)
добавлять только если A4 оставлен включённым.

```bash
printf '%s\n' FEATURE_APIFY_FACEBOOK=false APIFY_FACEBOOK_SCOPE=campaign APIFY_FACEBOOK_MAX_POSTS_PER_RUN=500 APIFY_FACEBOOK_MAX_USD_PER_RUN=2 APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN=10 LEADS_PROVIDER=apify CAMPAIGN_BUDGET_USD=5 >> .env && docker compose --env-file .env up -d facebook-runner campaign-runner analysis-worker watchdog   # watchdog — если (ж) включён
```

## 5. Проверки после всего — `docs/v2/DEPLOY.md:554-575`

```bash
cd /opt/real-estate-bot
docker compose --env-file .env ps --format 'table {{.Service}}\t{{.Status}}'
docker compose --env-file .env logs --since=30m | grep -iE "traceback|critical" || echo "traceback нет"
curl -fsS http://127.0.0.1:8080/healthz && echo " healthz ok"
psqlc "select run_id, status, items_imported, billed_usd, launched_at from apify_runs order by launched_at desc limit 5"
```

Норма: все `healthy`, `traceback нет`, `healthz ok`; `billed_usd` совпадает с Apify Billing. Эффективные значения
флагов: `docker compose --env-file .env config | grep -E 'ACQUISITION_MODE|CARD_EXTRA_FIELDS|CAMPAIGN_BUDGET_USD|WATCHDOG_ENABLED'`
(только эти ключи, не секреты). Откат всего сразу (`docs/v2/DEPLOY.md:600`, `:604-606`):

```bash
cp .env.bak.<метка> .env && docker compose --env-file .env up -d && docker compose --env-file .env --profile ops rm -sf watchdog
```

## 6. Базовая линия G1–G9 (P0.4) — `docs/v2/GOLDEN_SET.md:27-49`

Общий потолок **$7** на девять кампаний. Бот считает бюджет только на одну кампанию (`bot/utils/costs.py:317-326`):
при `5` худший случай 9 × $5 = $45. Поэтому на время прогона `0.8` и лимит в Apify Console (Billing → Limits:
текущий расход месяца + $7). Нужны ссылки на посты O7 для G6/G8. Профили `collector`/`analysis` — как в разделе 4.
На время прогона также `APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST=0.5` (читает `campaign-runner`).

```bash
date -u +%FT%TZ                        # записать: начало прогона
printf '%s\n' CAMPAIGN_BUDGET_USD=0.8 APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST=0.5 >> .env
docker compose --env-file .env up -d campaign-runner analysis-worker facebook-runner watchdog   # watchdog — если (ж) включён
docker compose --env-file .env config | grep -E 'CAMPAIGN_BUDGET_USD|COMMENTS_MAX_USD_PER_POST'   # 0.8 и 0.5
```

Запускать G1…G9 по одной (тексты задач — `docs/v2/GOLDEN_SET.md:12-22`). После каждой:

```bash
psqlc "select id, left(source_text,60), state from campaigns order by created_at desc limit 1"
./scripts/eval_campaign.sh <campaign-uuid> > /root/eval-<campaign-uuid>.txt     # вне репозитория: там контакты
psqlc "select round(sum(cost_usd),2) usd from campaign_costs where kind='cost' and created_at > '<начало прогона>'"
```

Норма: каждая кампания ≤ $0.8 (бюджет проверяется до вызова, перерасход на один вызов возможен); сумма с начала
прогона < $7 — достигли $5.7, следующие не запускать. Числа (точных/отправлено, мусорных хостов, лидов, $, дата, id)
вписать в таблицу `docs/v2/GOLDEN_SET.md:12-22` по шагам `:39-48`. После прогона (и при откате) вернуть O10:

```bash
printf '%s\n' CAMPAIGN_BUDGET_USD=5 APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST=1.0 >> .env && docker compose --env-file .env up -d campaign-runner analysis-worker facebook-runner watchdog   # watchdog — если (ж) включён
```

Лимит в Apify Console вернуть на прежний (O2). Итог мне: хэш коммита после `update.sh`, вывод п. 2, `apify_runs` A4,
заполненная таблица G1–G9.
