---
name: deploy-vps
description: Команды деплоя и отката на VPS бота (/opt/real-estate-bot) — бэкап, update.sh, проверка миграций и сервисов, включение флагов, cron. Используй, когда изменения смержены в main и нужно выкатить или откатить.
---

# deploy-vps

Доступа к серверу нет — выдай владельцу блоки команд и что считать нормой.

1. Бэкап (обязательно, с проверкой размера):
   ```bash
   cd /opt/real-estate-bot && set -o pipefail
   docker compose --env-file .env exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
     | gzip > ~/backups/db-$(date +%F-%H%M).sql.gz && ls -lh ~/backups | tail -1
   ```
2. Обновление: `BRANCH=main ./scripts/update.sh 2>&1 | grep -vE '^ =>|NOTICE|Already applied'`
   (код → образы → миграции → перезапуск). Норма: строка нужного merge-коммита, `Migrations are current` или
   `Applied: NNN_…`, все сервисы `Up`, `Caddy healthz: OK`.
3. Проверка: `schema_migrations`, `docker compose --env-file .env ps`,
   `logs --tail=50 <сервисы> | grep -iE "error|traceback" || echo ok`.
4. Флаги: `cp .env .env.bak.$(date +%F-%H%M)`, дописать переменные, `docker compose --env-file .env up -d <сервис>`.
   Для каждого флага — команда отката (флаг в `false`, тот же `up -d`).
5. Откат кода: `git checkout <предыдущий-коммит> && docker compose --env-file .env up -d --build`; миграции только
   добавляющие. Восстановление БД из бэкапа — только в крайнем случае.
