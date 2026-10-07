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

# --- Facebook browser stack: supervised Chrome + virtual display + remote viewer ---
#
# Only started when FACEBOOK_ENABLED=true. Xvfb gives Chrome somewhere to
# render without a real screen. Chrome itself is launched here, not by
# Playwright: FACEBOOK_CDP_URL tells the bot to attach to this already-running
# Chrome over CDP instead of launching a second one (see
# bot/services/facebook/browser.py), so there is exactly one browser process
# and one profile -- the noVNC view below and the bot's own automation are
# always looking at the same window. x11vnc serves that display over VNC on
# loopback only; websockify fronts it with noVNC's browser-based client, also
# loopback only.
#
# None of Xvfb, x11vnc, websockify, or Chrome's remote-debugging port is ever
# exposed publicly. The only thing meant to leave this container is the bot's
# own token-gated proxy (bot/services/facebook/gate.py, FACEBOOK_GATE_PORT,
# default 8090) -- point a Tailscale Funnel at that port (see the README's
# "Tailscale Funnel setup" section), never at 6080 or the CDP port directly.
# The admin never runs ssh, tailscale, or anything else at a terminal; they
# tap a Telegram button that opens a link straight to that gate.
#
# NOT run-tested in a real container as of this commit (no Docker available
# where this was written) -- watch this block's output on first deploy.
extra_pids=()
if [[ "${FACEBOOK_ENABLED:-false}" == "true" ]]; then
    export DISPLAY=":${FACEBOOK_DISPLAY_NUM:-99}"

    echo "entrypoint: starting Xvfb on ${DISPLAY}" >&2
    Xvfb "${DISPLAY}" -screen 0 1280x900x24 -nolisten tcp &
    xvfb_pid=$!
    extra_pids+=("${xvfb_pid}")

    for _ in $(seq 1 30); do
        [[ -e "/tmp/.X11-unix/X${DISPLAY#:}" ]] && break
        sleep 0.5
    done

    : "${FACEBOOK_PROFILE_DIR:=/app/data/facebook_profile}"
    : "${FACEBOOK_CDP_PORT:=9222}"
    mkdir -p "${FACEBOOK_PROFILE_DIR}"

    echo "entrypoint: starting Chrome on ${DISPLAY}, CDP on 127.0.0.1:${FACEBOOK_CDP_PORT}" >&2
    google-chrome-stable \
        --remote-debugging-port="${FACEBOOK_CDP_PORT}" \
        --remote-debugging-address=127.0.0.1 \
        --user-data-dir="${FACEBOOK_PROFILE_DIR}" \
        --no-first-run --no-default-browser-check \
        --window-size=1280,900 \
        about:blank &
    chrome_pid=$!
    extra_pids+=("${chrome_pid}")

    echo "entrypoint: starting x11vnc on 127.0.0.1:5900 (display ${DISPLAY})" >&2
    x11vnc -display "${DISPLAY}" -rfbport 5900 -localhost -forever -shared -nopw -quiet &
    x11vnc_pid=$!
    extra_pids+=("${x11vnc_pid}")

    echo "entrypoint: starting noVNC (websockify) on 127.0.0.1:6080" >&2
    websockify --web=/usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 &
    novnc_pid=$!
    extra_pids+=("${novnc_pid}")

    # Tell the bot how to reach the Chrome just started, unless the operator
    # already set FACEBOOK_CDP_URL explicitly (e.g. pointing at a Chrome
    # supervised outside this container).
    : "${FACEBOOK_CDP_URL:=http://127.0.0.1:${FACEBOOK_CDP_PORT}}"
    export FACEBOOK_CDP_URL
fi

echo "entrypoint: starting the bot" >&2
python -m bot.main &
bot_pid=$!

terminate() {
    echo "entrypoint: shutting down" >&2
    kill -TERM "${bot_pid}" "${searxng_pid}" "${extra_pids[@]:-}" 2>/dev/null || true
    wait "${bot_pid}" "${searxng_pid}" "${extra_pids[@]:-}" 2>/dev/null || true
    exit 0
}
trap terminate SIGTERM SIGINT

# Exit as soon as either main process does, carrying its status out. The
# Facebook browser stack (extra_pids) is supervised but not load-bearing for
# the container's own liveness -- Chrome/Xvfb/x11vnc/websockify dying does
# not stop the bot from serving non-Facebook requests, it just breaks the
# Facebook live-view and automation until the container restarts.
wait -n "${searxng_pid}" "${bot_pid}"
status=$?
echo "entrypoint: a process exited with status ${status}; stopping the container" >&2
kill -TERM "${bot_pid}" "${searxng_pid}" "${extra_pids[@]:-}" 2>/dev/null || true
exit "${status}"
