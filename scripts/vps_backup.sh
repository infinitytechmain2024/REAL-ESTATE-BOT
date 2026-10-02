#!/usr/bin/env bash
# Back up everything a reinstall must not lose, on the OLD server, before it is wiped:
#   - the PostgreSQL database (campaigns, sites approved by people, contacts, settings, migrations applied)
#   - the browser profiles volume (the signed-in Facebook / LinkedIn / X / TikTok / Instagram sessions)
#   - .env (all keys and settings)
# Usage (from the project directory):  sudo ./scripts/vps_backup.sh [output.tar]
# Copy the result off the server:      scp root@OLD_IP:/root/bot-backup-*.tar .
# The archive holds live logins and keys: keep it private, delete it when the new server runs.
set -euo pipefail

# The project is the current directory when it holds the stack, else the directory above this script.
if [[ -f docker-compose.yml && -f .env ]]; then
  project_dir="$(pwd)"
else
  project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
cd "$project_dir"
out="${1:-/root/bot-backup-$(date +%Y%m%d-%H%M).tar}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
compose=(docker compose --env-file .env)

[[ -f .env ]] || { echo "No .env in $project_dir" >&2; exit 2; }
project="$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"

echo "1/3 database"
"${compose[@]}" up -d postgres >/dev/null
"${compose[@]}" exec -T postgres sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$work/db.dump"

echo "2/3 browser profiles (signed-in sessions)"
# The browser must not write while the profile is copied.
"${compose[@]}" stop browser >/dev/null 2>&1 || true
docker run --rm -v "${project}_browser_profiles:/profiles:ro" -v "$work:/out" alpine \
  tar --numeric-owner -czf /out/browser_profiles.tgz -C /profiles .
"${compose[@]}" start browser >/dev/null 2>&1 || true

echo "3/3 .env"
cp .env "$work/env"

tar -cf "$out" -C "$work" db.dump browser_profiles.tgz env
chmod 600 "$out"
echo "Backup written: $out ($(du -h "$out" | cut -f1))"
