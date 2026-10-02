#!/usr/bin/env bash
# Full install of the bot on a clean Ubuntu 22.04/24.04 x86_64 VPS (also safe to run again to update).
#
#   curl -fsSL https://raw.githubusercontent.com/infinitytechmain2024/REAL-ESTATE-BOT/main/scripts/vps_install.sh -o vps_install.sh
#   sudo bash vps_install.sh                         # fresh install, asks for the keys
#   sudo BACKUP=/root/bot-backup-....tar bash vps_install.sh   # reinstall from scripts/vps_backup.sh
#
# Environment (all optional): REPO (git URL), BRANCH (default main), DIR (default /opt/real-estate-bot),
# BACKUP (archive from vps_backup.sh: database, signed-in browser profiles and .env are restored).
#
# It installs Docker, a swap file on small machines, the firewall (SSH, 80, 443 only), clones the code,
# writes .env with generated passwords (keys are asked for, never echoed), applies the database
# migrations, builds and starts every service, and prints what to do next in Telegram.
set -euo pipefail

REPO="${REPO:-https://github.com/infinitytechmain2024/REAL-ESTATE-BOT.git}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/real-estate-bot}"
BACKUP="${BACKUP:-}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
[[ "$(uname -m)" == "x86_64" ]] || die "x86_64 (amd64) only: Chrome is not built for $(uname -m)"
[[ -z "$BACKUP" || -f "$BACKUP" ]] || die "BACKUP=$BACKUP does not exist"

say "1/8 system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates python3 ufw >/dev/null

say "2/8 Docker"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null

say "3/8 swap and firewall"
mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
if [[ $mem_gb -lt 12 ]] && ! swapon --show | grep -q .; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "swap: 4G added (RAM ${mem_gb}G)"
fi
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null    # Caddy: the HTTPS certificate for the login window
ufw allow 443/tcp >/dev/null   # Caddy: the login window (Telegram Mini App), nothing else is public
ufw --force enable >/dev/null

say "4/8 code ($BRANCH)"
if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" pull -q --ff-only origin "$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"
chmod +x scripts/*.sh

say "5/8 .env"
set_env() {  # set_env NAME VALUE: replace (or append) one line, value never printed
  python3 - "$1" "$2" <<'PY'
import re, sys
from pathlib import Path
name, value = sys.argv[1], sys.argv[2]
path = Path(".env")
text = path.read_text()
line = f"{name}={value}"
pattern = re.compile(rf"^#?\s*{re.escape(name)}=.*$", re.M)
text, n = pattern.subn(lambda _m: line, text, count=1)
if not n:
    text = text.rstrip("\n") + "\n" + line + "\n"
path.write_text(text)
PY
}
secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(36))'; }
ask() {  # ask VAR "prompt" [hidden]
  local value=""
  while [[ -z "$value" ]]; do
    if [[ "${3:-}" == hidden ]]; then read -rsp "$2: " value; echo; else read -rp "$2: " value; fi
  done
  printf -v "$1" '%s' "$value"
}

if [[ -n "$BACKUP" && ! -f .env ]]; then
  tar -xOf "$BACKUP" env > .env
  echo ".env restored from the backup"
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  pg="$(secret)"; rd="$(secret)"
  set_env POSTGRES_PASSWORD "$pg"
  set_env DATABASE_URL "postgresql://monitoring_app:${pg}@postgres:5432/monitoring"
  set_env REDIS_PASSWORD "$rd"
  set_env REDIS_URL "redis://:${rd}@redis:6379/0"
  set_env BROWSER_SESSION_API_TOKEN "$(secret)"
  set_env SEARXNG_SECRET "$(secret)"
  echo "Passwords and internal tokens generated."
  ask TG "Telegram bot token (from @BotFather)" hidden
  ask OWNERS "Your Telegram user ID(s), comma separated (from @userinfobot)"
  ask OR "OpenRouter API key (sk-or-...)" hidden
  set_env TELEGRAM_TOKEN "$TG"
  set_env TELEGRAM_OPERATOR_IDS "$OWNERS"
  set_env OPENROUTER_API_KEY "$OR"
  read -rp "Residential proxy for X, optional (http://user:pass@host:port, Enter to skip): " XP
  [[ -n "$XP" ]] && set_env X_SEARCH_PROXY "$XP"
fi
# The login window (Telegram Mini App) needs an HTTPS name for this server: <ip>.sslip.io works at once.
if ! grep -q '^LIVE_VIEW_PUBLIC_URL=https://' .env; then
  ip="$(curl -fsS4 https://api.ipify.org || curl -fsS4 https://ifconfig.me)"
  domain="${ip//./-}.sslip.io"
  set_env LIVE_VIEW_DOMAIN "$domain"
  set_env LIVE_VIEW_PUBLIC_URL "https://$domain"
  set_env LIVE_VIEW_BIND "0.0.0.0"
  echo "Login window: https://$domain"
elif [[ -n "$BACKUP" ]]; then
  # A new server has a new address: the restored .env still names the old one.
  ip="$(curl -fsS4 https://api.ipify.org || curl -fsS4 https://ifconfig.me)"
  old="$(grep '^LIVE_VIEW_DOMAIN=' .env | cut -d= -f2-)"
  if [[ "$old" == *sslip.io && "$old" != "${ip//./-}.sslip.io" ]]; then
    domain="${ip//./-}.sslip.io"
    set_env LIVE_VIEW_DOMAIN "$domain"
    set_env LIVE_VIEW_PUBLIC_URL "https://$domain"
    echo "Login window moved to the new address: https://$domain"
  fi
fi
chmod 600 .env
compose=(docker compose --env-file .env)
"${compose[@]}" config -q || die ".env is incomplete: docker compose config failed"

say "6/8 database"
"${compose[@]}" up -d postgres redis
for _ in $(seq 1 60); do
  "${compose[@]}" exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1 && break
  sleep 2
done
if [[ -n "$BACKUP" ]]; then
  if [[ "$("${compose[@]}" exec -T postgres sh -c 'psql -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select to_regclass('"'"'public.campaigns'"'"') is not null"')" == "t" ]]; then
    echo "The database already has data: the backup is NOT restored over it."
  else
    tar -xOf "$BACKUP" db.dump | "${compose[@]}" exec -T postgres sh -c \
      'pg_restore --no-owner --role="$POSTGRES_USER" -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
      || echo "pg_restore reported warnings (usually harmless); check the bot after start."
    echo "database restored"
  fi
fi
./scripts/apply_migrations.sh

say "7/8 browser profiles"
project="$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
if [[ -n "$BACKUP" ]]; then
  docker volume create "${project}_browser_profiles" >/dev/null
  if [[ -z "$(docker run --rm -v "${project}_browser_profiles:/p" alpine ls -A /p)" ]]; then
    tar -xOf "$BACKUP" browser_profiles.tgz | docker run --rm -i -v "${project}_browser_profiles:/p" alpine \
      tar --numeric-owner -xzpf - -C /p
    echo "signed-in sessions restored"
  else
    echo "browser profiles already present: not overwritten"
  fi
fi

say "8/8 build and start (the first build takes 10-20 minutes)"
"${compose[@]}" up -d --build --remove-orphans
sleep 20
"${compose[@]}" ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
curl -fsS http://127.0.0.1:8080/healthz >/dev/null && echo "gateway: ok"

cat <<EOF

Done. Next, in Telegram:
  1. Send /start to your bot.
  2. Settings -> «🔐 Вход в соцсети»: sign in to Facebook, LinkedIn, X, TikTok, Instagram
     (spare accounts). The bot also sends the login link by itself when a search needs a platform.
  3. Give a test task in each mode and approve the site list it sends.

Logs:     cd $DIR && docker compose logs -f --tail=100 campaign-runner telegram browser
Update:   sudo BRANCH=$BRANCH bash $DIR/scripts/vps_install.sh
Backup:   sudo $DIR/scripts/vps_backup.sh
EOF
