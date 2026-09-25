#!/usr/bin/env bash
# Apply repository migrations in order, exactly once per database. This script
# never removes Docker volumes and refuses a changed migration checksum.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

env_file="${ENV_FILE:-.env}"
if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file. Copy .env.example and set strong POSTGRES_PASSWORD and REDIS_PASSWORD." >&2
  exit 2
fi

compose=(docker compose --env-file "$env_file")
for required in 001_init.sql 002_facebook.sql 003_orchestration.sql 004_telegram_control_plane.sql 005_orchestra_dispatcher.sql 006_openrouter_transcription.sql 007_live_view_sessions.sql 008_analysis_pipeline.sql 009_verification_flow.sql 010_verification_telegram_identity.sql 011_operator_access_requests.sql 012_collector_launch_requests.sql 013_analysis_claims.sql 014_campaigns.sql 015_campaign_groups.sql 016_campaign_runs.sql 017_control_settings.sql 018_user_role_task_drafts.sql; do
  [[ -f "bot/services/db/migrations/$required" ]] || {
    echo "Required migration is missing: $required" >&2
    exit 2
  }
done

"${compose[@]}" up -d postgres
"${compose[@]}" exec -T postgres sh -ec \
  'until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null; do sleep 1; done'

checksum() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

for migration_path in bot/services/db/migrations/001_init.sql \
                      bot/services/db/migrations/002_facebook.sql \
                      bot/services/db/migrations/003_orchestration.sql \
                      bot/services/db/migrations/004_telegram_control_plane.sql \
                      bot/services/db/migrations/005_orchestra_dispatcher.sql \
                      bot/services/db/migrations/006_openrouter_transcription.sql \
                      bot/services/db/migrations/007_live_view_sessions.sql \
                      bot/services/db/migrations/008_analysis_pipeline.sql \
                      bot/services/db/migrations/009_verification_flow.sql \
                      bot/services/db/migrations/010_verification_telegram_identity.sql \
                      bot/services/db/migrations/011_operator_access_requests.sql \
                      bot/services/db/migrations/012_collector_launch_requests.sql \
                      bot/services/db/migrations/013_analysis_claims.sql \
                      bot/services/db/migrations/014_campaigns.sql \
                      bot/services/db/migrations/015_campaign_groups.sql \
                      bot/services/db/migrations/016_campaign_runs.sql \
                      bot/services/db/migrations/017_control_settings.sql \
                      bot/services/db/migrations/018_user_role_task_drafts.sql; do
  migration="$(basename "$migration_path")"
  digest="$(checksum "$migration_path")"
  existing="$("${compose[@]}" exec -T postgres sh -ec \
    'psql -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
create table if not exists public.schema_migrations (
  filename text primary key,
  checksum text not null,
  applied_at timestamptz not null default now()
);
select checksum from public.schema_migrations where filename = '$migration';
SQL
)"

  if [[ -n "$existing" ]]; then
    [[ "$existing" == "$digest" ]] || {
      echo "Refusing changed already-applied migration: $migration" >&2
      exit 3
    }
    echo "Already applied: $migration"
    continue
  fi

  echo "Applying: $migration"
  # One transaction includes the advisory lock, migration, and registry write.
  # `/migrations` is read-only inside PostgreSQL, mounted by docker-compose.
  "${compose[@]}" exec -T postgres sh -ec \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
begin;
select pg_advisory_xact_lock(hashtext('real-estate-monitor:migrations'));
create table if not exists public.schema_migrations (filename text primary key, checksum text not null, applied_at timestamptz not null default now());
\\i /migrations/$migration
insert into public.schema_migrations (filename, checksum) values ('$migration', '$digest');
commit;
SQL
done

echo "Migrations are current."
