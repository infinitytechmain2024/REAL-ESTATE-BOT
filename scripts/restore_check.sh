#!/usr/bin/env bash
# Proves the latest backup restores: loads it into a throwaway postgres:16 container
# (tmpfs data, no ports, no network to the bot) and counts campaigns.
#   ./scripts/restore_check.sh            # latest file in BACKUP_DIR
#   ./scripts/restore_check.sh FILE.sql.gz
# The working database is never touched. Prints "OK, campaigns=N".
set -euo pipefail

backup_dir="${BACKUP_DIR:-/root/backups}"
name="real-estate-restore-check"

file="${1:-}"
if [[ -z "$file" ]]; then
  file="$(ls -1t "$backup_dir"/bot-*.sql.gz 2>/dev/null | head -n 1 || true)"
fi
if [[ -z "$file" || ! -f "$file" ]]; then
  echo "No backup found in $backup_dir" >&2
  exit 1
fi
gzip -t "$file"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name "$name" --network none \
  -e POSTGRES_PASSWORD=restore-check -e POSTGRES_USER=restore -e POSTGRES_DB=restore \
  --tmpfs /var/lib/postgresql/data postgres:16 >/dev/null

# The official image restarts once during init: require two consecutive "ready" answers.
ready=0
for _ in $(seq 1 60); do
  if docker exec "$name" pg_isready -U restore -d restore >/dev/null 2>&1; then
    sleep 3
    if docker exec "$name" pg_isready -U restore -d restore >/dev/null 2>&1; then ready=1; break; fi
  fi
  sleep 1
done
if (( ready == 0 )); then echo "Restore container did not start" >&2; exit 1; fi

# The dump has no owners or grants (--no-owner --no-privileges), so it loads under the temporary user.
gunzip -c "$file" | docker exec -i "$name" psql -q -v ON_ERROR_STOP=1 -U restore -d restore >/dev/null

count="$(docker exec "$name" psql -qAt -U restore -d restore -c 'select count(*) from campaigns')"
echo "OK, campaigns=$count"
