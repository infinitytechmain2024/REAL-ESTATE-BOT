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
# A real, visible Google Chrome -- not just Playwright's bundled Chromium --
# rendering into a virtual framebuffer (Xvfb), viewable remotely over VNC
# through noVNC's browser-based client. This is what the operator connects to
# for login/verification once the bot has no physical screen; see
# docker/entrypoint.sh and bot/handlers/facebook_admin.py.
#
# Deliberately NOT built or run-tested against a real container here -- there
# is no Docker available in this environment. Verify with `docker compose up
# --build` on the actual deployment host before relying on it; the package
# names below are standard, long-standing Debian packages, but "should exist"
# is not "confirmed working."
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        fonts-liberation \
        fonts-noto-color-emoji \
        wget \
        gnupg \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

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
