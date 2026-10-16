#!/usr/bin/env bash
# Database backup: pg_dump | gzip through the running postgres container.
#   ./scripts/backup.sh
# Env: BACKUP_DIR (default /root/backups), BACKUP_KEEP_DAYS (default 14), ENV_FILE (default .env).
# A dump of 10 KB or less is treated as broken: the file is removed and the exit code is 1.
# The only thing ever deleted is old bot-*.sql.gz files inside BACKUP_DIR.
set -euo pipefail
umask 077  # dumps hold personal data: owner-only files

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

env_file="${ENV_FILE:-.env}"
backup_dir="${BACKUP_DIR:-/root/backups}"
keep_days="${BACKUP_KEEP_DAYS:-14}"
min_bytes=10240

if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file" >&2
  exit 2
fi
case "$keep_days" in ''|*[!0-9]*) echo "BACKUP_KEEP_DAYS must be a number" >&2; exit 2;; esac

mkdir -p "$backup_dir"
out="$backup_dir/bot-$(date +%Y%m%d-%H%M%S).sql.gz"
tmp="$out.part"
trap 'rm -f -- "$tmp"' EXIT

docker compose --env-file "$env_file" exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' | gzip > "$tmp"

size="$(wc -c < "$tmp")"
if (( size <= min_bytes )); then
  echo "Backup too small ($size bytes <= $min_bytes): discarded" >&2
  exit 1
fi
gzip -t "$tmp"
mv -- "$tmp" "$out"
echo "OK: $out ($size bytes)"

# Rotation: only our own files, only inside BACKUP_DIR.
find "$backup_dir" -maxdepth 1 -type f -name 'bot-*.sql.gz' -mtime "+$keep_days" -print -delete
