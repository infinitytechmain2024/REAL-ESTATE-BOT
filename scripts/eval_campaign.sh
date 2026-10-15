#!/usr/bin/env bash
# Оценка кампании (только чтение): ./scripts/eval_campaign.sh <campaign-uuid>
# Запускать на VPS в /opt/real-estate-bot. SQL: scripts/eval_campaign.sql. Золотой набор: docs/v2/GOLDEN_SET.md.
set -euo pipefail
cd "$(dirname "$0")/.."

cid="${1:-}"
if [[ ! "$cid" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
  echo "usage: $0 <campaign-uuid>   (последние кампании: select id, left(source_text,60), state from campaigns order by created_at desc limit 5)" >&2
  exit 2
fi

# default_transaction_read_only: сервер сам откажет любой записи, даже если SQL-файл изменят.
docker compose --env-file .env exec -T postgres sh -c \
  'PGOPTIONS="-c default_transaction_read_only=on" psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v cid="$1" -f -' sh "$cid" \
  < scripts/eval_campaign.sql
