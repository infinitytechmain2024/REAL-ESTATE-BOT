#!/usr/bin/env bash
# Update the bot on the VPS in one go:
#   1. the latest code from GitHub (fast-forward only: local edits stop the update),
#   2. fresh images (postgres, redis, caddy, searxng) and rebuilt bot images on fresh bases,
#   3. database migrations (scripts/apply_migrations.sh),
#   4. every container recreated and restarted,
#   5. old images removed, then the state of every service.
#
# Usage:  ./scripts/update.sh                 # the branch the checkout is on
#         BRANCH=main ./scripts/update.sh     # switch to / update another branch
#         ENV_FILE=.env.prod ./scripts/update.sh
# Volumes (database, Facebook profile, Caddy certificates) are never removed.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

env_file="${ENV_FILE:-.env}"
if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file. Run: python3 scripts/setup_env.py" >&2
  exit 2
fi
compose=(docker compose --env-file "$env_file")
step() { printf '\n==> %s\n' "$*"; }

step "Code"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Local changes in tracked files; commit or stash them first:" >&2
  git status --short --untracked-files=no >&2
  exit 3
fi
branch="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
before="$(git rev-parse HEAD)"
git fetch origin "$branch"
git checkout -q "$branch"
git merge --ff-only "origin/$branch"
after="$(git rev-parse HEAD)"
if [[ "$before" == "$after" ]]; then
  echo "Already on the latest commit of $branch: $(git log -1 --format='%h %s')"
else
  git log --format='  %h %s' "$before..$after"
fi

step "Images"
"${compose[@]}" pull --ignore-buildable
"${compose[@]}" build --pull

step "Database migrations"
./scripts/apply_migrations.sh

step "Restart"
"${compose[@]}" up -d --force-recreate

step "Cleanup"
docker image prune -f

step "Status"
sleep 10
"${compose[@]}" ps
if curl -fsS --max-time 5 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
  echo "Caddy healthz: OK"
else
  echo "Caddy healthz: no answer yet (check: ${compose[*]} logs --tail=50)"
fi
echo
echo "Done. Logs: docker compose logs -f --tail=100"
