#!/usr/bin/env bash
# Start SearXNG and the bot in one container.
#
# SearXNG runs as a WSGI app on loopback; the bot talks to it over
# http://127.0.0.1:8888 and is the only thing that can reach it. Neither
# process is useful alone, so if either exits the container exits -- that way
# the platform's restart policy handles it instead of the container sitting
# there half-alive.

set -euo pipefail

: "${SEARXNG_PORT:=8888}"
: "${SEARXNG_BIND_ADDRESS:=127.0.0.1}"
: "${SEARXNG_SETTINGS_PATH:=/app/searxng/settings/settings.yml}"
export SEARXNG_PORT SEARXNG_BIND_ADDRESS SEARXNG_SETTINGS_PATH

if [[ -z "${SEARXNG_SECRET:-}" ]]; then
    # Required by SearXNG. Nothing user-facing is signed with it here (there is
    # no web UI and no sessions), so a per-boot value is fine and beats
    # shipping a default that every deployment shares.
    SEARXNG_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
    export SEARXNG_SECRET
    echo "entrypoint: SEARXNG_SECRET was not set, generated an ephemeral one" >&2
fi

echo "entrypoint: starting SearXNG on ${SEARXNG_BIND_ADDRESS}:${SEARXNG_PORT}" >&2
granian \
    --interface wsgi \
    --host "${SEARXNG_BIND_ADDRESS}" \
    --port "${SEARXNG_PORT}" \
    --workers "${SEARXNG_WORKERS:-1}" \
    searxng.api_only:application &
searxng_pid=$!

# Wait for the port, but do not block forever: the bot warns and keeps running
# if SearXNG is not up yet, and engines initialise lazily anyway.
for _ in $(seq 1 30); do
    if python -c "
import socket, sys
sock = socket.socket()
sock.settimeout(0.5)
sys.exit(0 if sock.connect_ex(('127.0.0.1', ${SEARXNG_PORT})) == 0 else 1)
" 2>/dev/null; then
        echo "entrypoint: SearXNG is accepting connections" >&2
        break
    fi
    if ! kill -0 "${searxng_pid}" 2>/dev/null; then
        echo "entrypoint: SearXNG exited during start-up" >&2
        exit 1
    fi
    sleep 1
done

echo "entrypoint: starting the bot" >&2
python -m bot.main &
bot_pid=$!

terminate() {
    echo "entrypoint: shutting down" >&2
    kill -TERM "${bot_pid}" "${searxng_pid}" 2>/dev/null || true
    wait "${bot_pid}" "${searxng_pid}" 2>/dev/null || true
    exit 0
}
trap terminate SIGTERM SIGINT

# Exit as soon as either process does, carrying its status out.
wait -n "${searxng_pid}" "${bot_pid}"
status=$?
echo "entrypoint: a process exited with status ${status}; stopping the container" >&2
kill -TERM "${bot_pid}" "${searxng_pid}" 2>/dev/null || true
exit "${status}"
