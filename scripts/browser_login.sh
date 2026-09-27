#!/usr/bin/env bash
# Log a browser profile into its platform, or clear a Facebook checkpoint, by
# hand through a temporary noVNC view. Run on the VPS from the project root:
#
#   bash scripts/browser_login.sh [platform] [profile-name] [url] [minutes]
#   bash scripts/browser_login.sh facebook facebook-main
#
# It creates the browser_profiles row if needed, opens the browser under the
# same lease/lock collectors use (so it cannot run during a batch), and after
# you confirm, marks the profile ready and resolves open verification holds.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

platform="${1:-facebook}"
profile_name="${2:-${platform}-main}"
url="${3:-https://www.facebook.com/}"
minutes="${4:-20}"
env_file="${ENV_FILE:-.env}"
local_port=6090  # on the operator's own computer

case "$platform" in facebook|instagram|tiktok|linkedin|website) ;; *)
  echo "platform must be facebook, instagram, tiktok, linkedin, or website" >&2; exit 2 ;;
esac
# Interpolated into SQL below, so only a safe identifier is accepted.
[[ "$profile_name" =~ ^[a-z0-9_-]{1,64}$ ]] || {
  echo "profile name may contain only a-z, 0-9, '_' and '-'" >&2; exit 2; }
[[ "$minutes" =~ ^[0-9]+$ ]] || { echo "minutes must be a number" >&2; exit 2; }
[[ -f "$env_file" ]] || { echo "Missing $env_file" >&2; exit 2; }

compose=(docker compose --env-file "$env_file")
psql_exec() {
  "${compose[@]}" exec -T postgres sh -ec \
    'psql -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

"${compose[@]}" up -d postgres browser >/dev/null

row="$(psql_exec <<SQL
begin;
\\o /dev/null
select set_config('app.actor', 'operator:browser_login', true);
\\o
insert into public.browser_profiles (profile_name, platform, storage_locator)
values ('$profile_name', '$platform', 'volume:browser_profiles')
on conflict (profile_name) do nothing;
select id || '|' || platform || '|' || state from public.browser_profiles
 where profile_name = '$profile_name' and deleted_at is null;
commit;
SQL
)"
row="$(printf '%s\n' "$row" | grep '|' | tail -n1)"
IFS='|' read -r profile_id profile_platform profile_state <<<"$row"
[[ -n "${profile_id:-}" ]] || { echo "Could not create or find profile $profile_name" >&2; exit 3; }
[[ "$profile_platform" == "$platform" ]] || {
  echo "Profile $profile_name belongs to $profile_platform, not $platform" >&2; exit 3; }
case "$profile_state" in
  in_use) echo "Profile $profile_name is in use by a collector; wait for it to finish." >&2; exit 3 ;;
  disabled|retired) echo "Profile $profile_name is $profile_state." >&2; exit 3 ;;
esac

# noVNC is never published on the host. The VPS reaches the container on its
# Docker bridge address, so the operator tunnels there over SSH.
container="$("${compose[@]}" ps -q browser)"
[[ -n "$container" ]] || { echo "The browser service is not running." >&2; exit 3; }
browser_ip="$(docker inspect -f '{{range $name, $net := .NetworkSettings.Networks}}{{$name}}={{$net.IPAddress}} {{end}}' "$container" \
  | tr ' ' '\n' | sed -n 's/^.*_egress=//p' | head -n1)"
[[ -n "$browser_ip" ]] || { echo "Could not find the browser container's address." >&2; exit 3; }

cat <<EOF

Profile $profile_name ($profile_id), currently $profile_state.
On your own computer, open a tunnel to the VPS and keep it running:

    ssh -N -L $local_port:$browser_ip:6080 <user>@<vps-host>

Then open http://localhost:$local_port/vnc.html and use the password printed below.
Nothing listens on that address once this session ends.

EOF

"${compose[@]}" exec browser python -m bot.browser_session.interactive \
  --profile-id "$profile_id" --platform "$platform" --url "$url" --minutes "$minutes" || {
  echo "The browser session failed; $profile_name was left as $profile_state." >&2; exit 4; }

read -r -p "Is $profile_name logged in with no checkpoint showing? Mark it ready [y/N] " answer
if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
  echo "Left $profile_name as $profile_state."
  exit 0
fi

psql_exec <<SQL
begin;
\\o /dev/null
select set_config('app.actor', 'operator:browser_login', true);
\\o
update public.browser_profiles set state = 'ready', last_verified_at = now()
 where id = '$profile_id' and state in ('provisioned', 'human_verification_required', 'quarantined');
-- A cleared checkpoint resolves the holds it created on this platform.
update public.verification_jobs j set state = 'active'
  from public.monitoring_sources s
 where s.id = j.source_id and s.platform = '$platform' and j.state = 'requested';
update public.verification_jobs j
   set state = 'verified', resolved_at = now(), resolved_by = 'operator:browser_login'
  from public.monitoring_sources s
 where s.id = j.source_id and s.platform = '$platform' and j.state = 'active';
update public.monitoring_sources set state = 'active'
 where platform = '$platform' and state = 'human_verification_required';
commit;
select 'profile: ' || state from public.browser_profiles where id = '$profile_id';
select 'batches still waiting on verification: ' || count(*) from public.acquisition_batches
 where platform = '$platform' and state = 'human_verification_required';
SQL
echo "Batches stopped by a checkpoint stay as they are: /cancel batch:<id> them and /run again."
