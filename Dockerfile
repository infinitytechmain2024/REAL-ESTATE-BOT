# One image, two processes: the SearXNG JSON API on loopback and the bot
# talking to it. See docker/entrypoint.sh for how they are supervised.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # searxng/ is a plain directory, not an installed package, so both it and
    # the project root have to be importable.
    PYTHONPATH=/app:/app/searxng

WORKDIR /app

# lxml and trafilatura need these to build/run; curl is the health check.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# --- Facebook browser stack (only used when FACEBOOK_ENABLED=true) -------
#
# A real, visible browser rendering into a virtual framebuffer (Xvfb),
# viewable remotely over VNC through noVNC's browser-based client. This is
# what the operator connects to for login/verification once the bot has no
# physical screen; see docker/entrypoint.sh and bot/handlers/facebook_admin.py.
#
# Brave rather than Google Chrome, and one system browser rather than two.
# Brave is Chromium underneath, so Playwright drives it through
# `executable_path` (there is no `channel="brave"`), and the same binary also
# serves the parser's fallback fetcher -- which is what lets this image skip
# `playwright install chromium` entirely. Before this, the image shipped
# Chrome for Facebook and nothing at all for the parser, so
# PARSER_BROWSER_ENABLED=true failed at start-up with "run playwright install
# chromium" on a machine where running it was never part of the build.
#
# The trade-off is real and worth stating: Chrome was chosen originally
# because a very common browser is the least interesting thing an anti-bot
# system can see, and Brave is both rarer and, by default, randomises
# fingerprints per session ("farbling") and blocks social embeds. Turn
# Shields off for facebook.com once, in the live view, during the same manual
# login that clears the first checkpoint -- it is one click in the address
# bar, and it sticks with the profile. If Facebook still proves sticky, set
# FACEBOOK_BROWSER_BINARY to a Chrome you install yourself; nothing below is
# load-bearing for that.
#
# Deliberately NOT built or run-tested against a real container here -- there
# is no Docker available in this environment, and the Brave apt host is
# blocked from it, so the repository layout below could not be checked either.
# Verify with `docker compose up --build` on the actual deployment host before
# relying on it.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        fonts-liberation \
        fonts-noto-color-emoji \
        ca-certificates \
    && curl -fsSL https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg \
        -o /usr/share/keyrings/brave-browser-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg] https://brave-browser-apt-release.s3.brave.com/ stable main" \
        > /etc/apt/sources.list.d/brave-browser-release.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y brave-browser \
    && rm -rf /var/lib/apt/lists/*

# Both browser users read these, so the binary is named in exactly one place.
# Unset them and the code falls back to what works on a developer laptop:
# `channel="chrome"` for Facebook, Playwright's bundled Chromium for the
# parser. Override them to run a different browser without touching code.
ENV FACEBOOK_BROWSER_BINARY=/usr/bin/brave-browser \
    PARSER_BROWSER_BINARY=/usr/bin/brave-browser

# Dependencies first so a code change does not invalidate the layer.
COPY requirements.txt ./
COPY searxng/requirements.txt ./searxng-requirements.txt
COPY searxng/requirements-server.txt ./searxng-server-requirements.txt
RUN pip install --no-cache-dir \
        -r requirements.txt \
        -r searxng-requirements.txt \
        -r searxng-server-requirements.txt

COPY searxng/ ./searxng/
COPY bot/ ./bot/
COPY docker/ ./docker/
# Small, and they make the image self-checking: after a deploy,
# `docker compose exec bot python scripts/gate_probe.py` validates the
# live-view path without installing anything on the host.
COPY scripts/ ./scripts/

RUN chmod +x docker/entrypoint.sh \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8888
# noVNC itself is never exposed, loopback-only inside the container (see
# docker/entrypoint.sh and docker-compose.yml). The port meant to be reachable
# from outside is the bot's own token-gated proxy in front of it
# (bot/services/facebook/gate.py) -- point a Tailscale Funnel at this one, see
# the README's "Tailscale Funnel setup" section. Never expose 6080 or the
# Chrome CDP port directly; the gate is the only intended boundary.
EXPOSE 8090

# Checks the SearXNG API rather than the bot: in polling mode the bot has no
# port, and a live SearXNG plus a running entrypoint means both are up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${SEARXNG_PORT:-8888}/healthz || exit 1

ENTRYPOINT ["./docker/entrypoint.sh"]
