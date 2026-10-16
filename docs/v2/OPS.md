# Эксплуатация 24/7

## Healthcheck и перезапуск

У каждого долгоживущего сервиса в `docker-compose.yml` есть `healthcheck` и `restart: unless-stopped`
(проверяет `tests/test_deployment.py`). Python-сервисы: `python -m bot.<пакет>.healthcheck`, раз в 60 с, таймаут 20 с,
3 попытки, старт 60 с (браузер 120 с). `campaign-runner`, `analysis-worker`, `reduction-worker`, `watchdog` проверяют
импорт и `select 1` к БД (5 с). `searxng`: `/healthz` на `127.0.0.1:8080`.

Состояние: `docker compose --env-file .env ps` (колонка `STATUS`: `healthy` / `unhealthy`).

## Watchdog (алерты владельцу в Telegram)

Сервис `watchdog` лежит в profile `ops` и по умолчанию выключен дважды: профиль не включён и `WATCHDOG_ENABLED=false`
(процесс только пишет в лог и спит). `scripts/update.sh` ничего про него не знает: Compose берёт профили из
`COMPOSE_PROFILES` в `.env`, так что после включения сервис обновляется и перезапускается вместе со всеми.

Что он смотрит каждые `WATCHDOG_POLL_SECONDS` (60 с):

* контейнеры проекта `real-estate-monitor` в состоянии `exited`, `restarting` или `unhealthy` (Docker Engine API
  через `/var/run/docker.sock`, смонтированный `:ro`; пустой `WATCHDOG_DOCKER_SOCKET` отключает эту проверку);
  если таких сразу 3 и больше, приходит один алерт «Проблемы у N сервисов»;
* БД: 3 ошибки Apify подряд, бюджет кампании исчерпан, 2 завершённые кампании подряд без карточек, портал вернул
  0 объявлений.

Один и тот же алерт не повторяется чаще, чем раз в `WATCHDOG_ALERT_COOLDOWN_MINUTES` (30). Кулдаун хранится в памяти:
после рестарта watchdog возможен один повтор. События «портал пуст» и «0 карточек ×2» приходят один раз на процесс.
Дневная сводка приходит один раз в сутки в `WATCHDOG_DAILY_SUMMARY_HOUR` (по UTC): кампании запущено/завершено,
карточки, лиды, потрачено $, ошибки. Токен и chat id в логи не пишутся.

### Включить

```bash
cd /opt/real-estate-bot && cp .env .env.bak.$(date +%F-%H%M)
stat -c %g /var/run/docker.sock        # номер группы docker, подставить в WATCHDOG_DOCKER_GID
cat >> .env <<'ENV'
COMPOSE_PROFILES=ops
WATCHDOG_ENABLED=true
WATCHDOG_DOCKER_SOCKET=/var/run/docker.sock
WATCHDOG_DOCKER_GID=<число из stat>
ENV
docker compose --env-file .env up -d --build watchdog
docker compose --env-file .env logs --tail=20 watchdog     # "watchdog started"
```

Алерты и сводка идут всем владельцам из `TELEGRAM_OPERATOR_IDS` (они должны хоть раз написать боту `/start`). Отдельный id не нужен;
`OWNER_ALERT_CHAT_ID=<chat id>` задавайте только чтобы слать в другой чат/группу (тогда только туда).

Без контроля контейнеров достаточно `COMPOSE_PROFILES=ops`, `WATCHDOG_ENABLED=true`
(`WATCHDOG_DOCKER_SOCKET` остаётся пустым: сокет примонтирован, но не используется).

### Откат

Мягкий (контейнер жив, спит): `WATCHDOG_ENABLED=false` в `.env`, затем `docker compose --env-file .env up -d watchdog`.
Полный (контейнера нет): удалить `COMPOSE_PROFILES=ops` из `.env` и
`docker compose --env-file .env --profile ops rm -sf watchdog`.

### Безопасность сокета

`:ro` на сокете запрещает только его пересоздание: Docker API через него остаётся полным, то есть процесс с сокетом
может запустить любой контейнер и получить root на хосте. Поэтому watchdog не публикует портов, работает под
отдельным пользователем с `no-new-privileges` и исполняет только код этого репозитория. Не нужен контроль контейнеров:
оставьте `WATCHDOG_DOCKER_SOCKET` пустым и не включайте профиль `ops` на машинах, где сокет недопустим.

## Алерты: что значит и что делать

| Алерт | Что значит | Что делать |
|---|---|---|
| `Сервис X: unhealthy` | Healthcheck 3 раза подряд не прошёл (БД не отвечает, браузер не поднялся, импорт сломан). | `docker compose --env-file .env logs --tail=50 X`; обычно помогает `docker compose --env-file .env restart X`. Если упала БД, смотреть `postgres`. |
| `Сервис X: exited` | Контейнер остановлен (упал или его остановили руками). | `logs --tail=50 X`, затем `docker compose --env-file .env up -d X`. Сознательно остановленный сервис будет алертить каждые 30 минут. |
| `Сервис X: restarting` | Цикл падений: Docker перезапускает его снова и снова. | `logs --tail=100 X`: чаще всего ошибка настроек или импорта после выкатки. Откат кода: см. skill `deploy-vps`, пункт 5. |
| `Проблемы у N сервисов` | Три и больше контейнеров сразу: общая причина (БД, диск, память, сеть). | `docker compose --env-file .env ps`, `df -h`, `free -m`, `logs --tail=50 postgres`. |
| `3 ошибки Apify подряд` | Последние три вызова Apify по кампании кончились ошибкой (токен, лимит, актор). | `SELECT code, count(*) FROM campaign_costs WHERE kind='error' AND stage='api' GROUP BY 1;`, проверить `APIFY_TOKEN` и баланс. Источник можно выключить флагом `APIFY_*_ENABLED=false`. |
| `бюджет исчерпан` | Платные вызовы кампании дошли до `CAMPAIGN_BUDGET_USD`; новые модельные вызовы по ней остановлены. | Если лимит ожидаем, ничего. Иначе поднять `CAMPAIGN_BUDGET_USD` в `.env` и `up -d` затронутые сервисы. |
| `2 завершённые кампании подряд без карточек` | Источники ничего не дали или фильтры режут всё. | Отчёт кампании в чате, `campaign_costs` (`kind='skip'` по кодам), логи `campaign-runner`. |
| `Портал P вернул 0 объявлений` | Актор отработал (`completed`), но объявлений нет: сменилась разметка или фильтр слишком узкий. | Проверить запуск в консоли Apify; сравнить с `docs/apify_portals.md`. |
| Нет ни алертов, ни сводки в 09:00 UTC | Watchdog не работает. | `docker compose --env-file .env ps watchdog`, `logs --tail=20 watchdog` (причина простоя пишется одной строкой). |

Проверка после включения: `docker compose --env-file .env stop analysis-worker` даёт алерт `exited` в течение
минуты-двух, `docker compose --env-file .env up -d analysis-worker` возвращает сервис. (`kill` не годится для проверки:
политика `unless-stopped` сразу поднимает убитый контейнер обратно, и состояние `exited` можно не застать.)

## Бэкапы

`scripts/backup.sh` делает `pg_dump | gzip` через `docker compose --env-file .env exec -T postgres` в
`/root/backups/bot-ГГГГММДД-ЧЧММСС.sql.gz` (`BACKUP_DIR` переопределяет каталог). Дамп не больше 10 КБ считается
сломанным: файл удаляется, код выхода 1. Старше `BACKUP_KEEP_DAYS` (14) дней удаляются только `bot-*.sql.gz` внутри
`BACKUP_DIR`. Секретов в дампе нет только в смысле `.env`: сами данные кампаний там есть, каталог держите `chmod 700`.
Новые дампы создаются с правами 0600 (`umask 077`); права старых дампов не меняются, а внешняя синхронизация не от root
их больше не увидит.

Первый запуск и проверка восстановления:

```bash
cd /opt/real-estate-bot
chmod 700 /root/backups 2>/dev/null || true
set -o pipefail; ./scripts/backup.sh && ls -lh /root/backups | tail -3
./scripts/restore_check.sh          # "OK, campaigns=N"
```

`restore_check.sh` поднимает временный `postgres:16` (`real-estate-restore-check`, данные в tmpfs, без сети и портов),
загружает последний бэкап, считает `campaigns` и удаляет контейнер (и при ошибке тоже). Рабочая БД не затрагивается.

### Расписание (cron root, `crontab -e`)

```cron
0 3 * * *   cd /opt/real-estate-bot && ./scripts/backup.sh >> /var/log/re-backup.log 2>&1 || echo "backup failed" >> /var/log/re-backup.log
30 4 * * 0  cd /opt/real-estate-bot && ./scripts/restore_check.sh >> /var/log/re-backup.log 2>&1
15 4 * * *  cd /opt/real-estate-bot && docker compose --env-file .env run --rm facebook-runner python -m bot.facebook_collector.apify.retention >> /var/log/re-backup.log 2>&1
```

Третья строка: ежедневная чистка `apify_raw_items` старше `APIFY_FACEBOOK_RAW_RETENTION_DAYS` (команда из
`docs/apify_adapter.md`; перед первым включением прогоните её с `--dry-run`). Аудит `apify_runs` и посты она не трогает.
Раз в неделю смотрите `tail -20 /var/log/re-backup.log`: должно быть `OK:` и `OK, campaigns=`. Отключить: закомментировать
строку в `crontab -e`. Сам `/var/log/re-backup.log` ротируйте через `logrotate` или периодически очищайте.

## Перезагрузка VPS

```bash
cd /opt/real-estate-bot
set -o pipefail; ./scripts/backup.sh                    # 1. бэкап; при ошибке дальше не идти
docker compose --env-file .env down                     # 2. остановка (тома не удаляются, без -v)
reboot                                                  # 3. после входа по SSH:
cd /opt/real-estate-bot && docker compose --env-file .env up -d     # 4. подъём
sleep 60
docker compose --env-file .env ps                       # 5. все сервисы Up, healthy
curl -fsS http://127.0.0.1:8080/healthz && echo healthz ok
docker compose --env-file .env logs --since=5m | grep -iE "error|traceback|critical" || echo "ошибок нет"
```

Сервисы с `restart: unless-stopped` после `down` сами не стартуют, поэтому `up -d` нужен. Если пропустить `down`,
Docker сам поднимет контейнеры после загрузки. Алерт watchdog о недоступности после перезагрузки допустим; если сервис
остался `unhealthy` через 3 минуты, смотрите `logs --tail=50 <сервис>`.

## Ротация логов

Все сервисы пишут через драйвер `json-file` с `max-size: "20m"`, `max-file: "5"` (якорь `x-logging` в
`docker-compose.yml`): не больше 100 МБ на контейнер. Настройка применяется при пересоздании контейнера, то есть после
`BRANCH=main ./scripts/update.sh`. Проверка:

```bash
docker inspect -f '{{.HostConfig.LogConfig}}' real-estate-monitor-postgres-1
df -h /var/lib/docker
```

Откат: убрать строку `logging: *logging` у нужного сервиса и пересоздать его (`up -d <сервис>`).
