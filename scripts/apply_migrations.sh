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
for required in 001_init.sql 002_facebook.sql 003_orchestration.sql 004_telegram_control_plane.sql 005_orchestra_dispatcher.sql 006_openrouter_transcription.sql 007_live_view_sessions.sql 008_analysis_pipeline.sql 009_verification_flow.sql 010_verification_telegram_identity.sql 011_operator_access_requests.sql 012_collector_launch_requests.sql 013_analysis_claims.sql 014_campaigns.sql 015_campaign_groups.sql 016_campaign_runs.sql 017_control_settings.sql 018_user_role_task_drafts.sql 019_campaign_near_matches.sql 020_campaign_excluded_findings.sql 021_campaign_web_search.sql 022_social_search.sql 023_campaign_finding_relevance.sql 024_agent_findings.sql 025_agent_reductions.sql 026_campaign_comment_leads.sql 027_investor_reach.sql 028_reach_company_kind.sql 029_web_search_snippets.sql 030_campaign_summary.sql 031_task_draft_steps.sql 032_web_seen_urls_ttl.sql 033_campaign_finding_hold_reason.sql 034_web_fetch_layers.sql 035_campaign_specs.sql 036_campaign_finding_clusters.sql 037_reach_enrichment.sql 038_finding_review.sql 039_campaign_metrics.sql 040_live_status.sql 041_web_verification.sql 042_campaign_costs.sql 043_listing_sources.sql 044_web_listing_source_runs.sql; do
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
                      bot/services/db/migrations/018_user_role_task_drafts.sql \
                      bot/services/db/migrations/019_campaign_near_matches.sql \
                      bot/services/db/migrations/020_campaign_excluded_findings.sql \
                      bot/services/db/migrations/021_campaign_web_search.sql \
                      bot/services/db/migrations/022_social_search.sql \
                      bot/services/db/migrations/023_campaign_finding_relevance.sql \
                      bot/services/db/migrations/024_agent_findings.sql \
                      bot/services/db/migrations/025_agent_reductions.sql \
                      bot/services/db/migrations/026_campaign_comment_leads.sql \
                      bot/services/db/migrations/027_investor_reach.sql \
                      bot/services/db/migrations/028_reach_company_kind.sql \
                      bot/services/db/migrations/029_web_search_snippets.sql \
                      bot/services/db/migrations/030_campaign_summary.sql \
                      bot/services/db/migrations/031_task_draft_steps.sql \
                      bot/services/db/migrations/032_web_seen_urls_ttl.sql \
                      bot/services/db/migrations/033_campaign_finding_hold_reason.sql \
                      bot/services/db/migrations/034_web_fetch_layers.sql \
                      bot/services/db/migrations/035_campaign_specs.sql \
                      bot/services/db/migrations/036_campaign_finding_clusters.sql \
                      bot/services/db/migrations/037_reach_enrichment.sql \
                      bot/services/db/migrations/038_finding_review.sql \
                      bot/services/db/migrations/039_campaign_metrics.sql \
                      bot/services/db/migrations/040_live_status.sql \
                      bot/services/db/migrations/041_web_verification.sql \
                      bot/services/db/migrations/042_campaign_costs.sql \
                      bot/services/db/migrations/043_listing_sources.sql \
                      bot/services/db/migrations/044_web_listing_source_runs.sql; do
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
