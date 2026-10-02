#!/usr/bin/env bash
# Full reinstall ON THE SAME SERVER, in one command, keeping the data and the social logins:
#   1. finds the current install (or none: then it is a fresh install),
#   2. backs up the database, the signed-in browser profiles and .env (scripts/vps_backup.sh),
#   3. checks the backup, then removes the old containers and their volumes,
#   4. installs the new version from scratch and restores the backup (scripts/vps_install.sh).
# No IP address to type: the server finds its own address for the login window.
#
#   curl -fsSL https://raw.githubusercontent.com/infinitytechmain2024/REAL-ESTATE-BOT/main/scripts/vps_reinstall.sh | sudo bash
#
# Optional: BRANCH (default main), DIR (default /opt/real-estate-bot), OLD_DIR (where the bot runs now).
set -euo pipefail

BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/real-estate-bot}"
RAW="https://raw.githubusercontent.com/infinitytechmain2024/REAL-ESTATE-BOT/$BRANCH/scripts"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root (sudo)"

# The current install: the one given, the default place, or wherever its docker-compose.yml is.
old="${OLD_DIR:-}"
if [[ -z "$old" ]]; then
  for candidate in "$DIR" /root/REAL-ESTATE-BOT /home/*/REAL-ESTATE-BOT /opt/REAL-ESTATE-BOT /srv/REAL-ESTATE-BOT; do
    if [[ -f "$candidate/docker-compose.yml" && -f "$candidate/.env" ]]; then old="$candidate"; break; fi
  done
fi

backup=""
if [[ -n "$old" ]]; then
  say "Current install: $old"
  backup="/root/bot-backup-$(date +%Y%m%d-%H%M%S).tar"
  curl -fsSL "$RAW/vps_backup.sh" -o /root/vps_backup.sh
  (cd "$old" && bash /root/vps_backup.sh "$backup")
  for part in db.dump browser_profiles.tgz env; do
    tar -tf "$backup" "$part" >/dev/null 2>&1 || die "backup $backup has no $part: nothing was removed"
  done
  [[ $(tar -xOf "$backup" db.dump | wc -c) -gt 1000 ]] || die "the database dump is empty: nothing was removed"
  say "Backup checked: $backup. Removing the old containers and volumes"
  (cd "$old" && docker compose --env-file .env down -v --remove-orphans)
  if [[ "$old" == "$DIR" ]]; then
    mv "$DIR" "${DIR}.old-$(date +%Y%m%d-%H%M%S)"
  fi
else
  say "No current install found: a fresh install"
fi

curl -fsSL "$RAW/vps_install.sh" -o /root/vps_install.sh
# The install asks for keys only on a fresh install; read them from the terminal, not from this pipe.
BRANCH="$BRANCH" DIR="$DIR" BACKUP="$backup" bash /root/vps_install.sh </dev/tty
say "Reinstall finished. The backup stays in ${backup:-(none)}; delete it once the bot works."
