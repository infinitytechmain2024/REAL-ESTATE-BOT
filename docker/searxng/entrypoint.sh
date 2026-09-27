#!/bin/sh
# Start the official SearXNG image with this repository's settings.
#
# docker/searxng is mounted read-only at /opt/bot-searxng. The settings are
# copied into the image's config directory on every start (so an edited file
# takes effect on restart), with an `outgoing.proxies` block when
# WEB_SEARCH_PROXY_URL is set, and a random secret when SEARXNG_SECRET is not.
# Then the image's own entrypoint runs. No value is ever printed.
set -eu

config="${__SEARXNG_CONFIG_PATH:-/etc/searxng}"
mkdir -p "$config"
awk -v proxy="${WEB_SEARCH_PROXY_URL:-}" '
  /^#__PROXIES__$/ {
    if (proxy != "") { print "  proxies:"; print "    all://:"; print "      - \"" proxy "\"" }
    next
  }
  { print }
' /opt/bot-searxng/settings.yml > "$config/settings.yml"

if [ -z "${SEARXNG_SECRET:-}" ]; then
  SEARXNG_SECRET="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  export SEARXNG_SECRET
fi

exec /usr/local/searxng/entrypoint.sh "$@"
