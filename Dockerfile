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

RUN chmod +x docker/entrypoint.sh \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8888

# Checks the SearXNG API rather than the bot: in polling mode the bot has no
# port, and a live SearXNG plus a running entrypoint means both are up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${SEARXNG_PORT:-8888}/healthz || exit 1

ENTRYPOINT ["./docker/entrypoint.sh"]
