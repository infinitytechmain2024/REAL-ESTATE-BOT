FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface
WORKDIR /app

# ffmpeg decodes Telegram's OGG/Opus voice notes. The process has no
# browser tooling and no host-facing port.
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/control_plane/ ./bot/control_plane/
COPY bot/orchestra/ ./bot/orchestra/
RUN mkdir -p /models && chown -R appuser:appuser /app /models
USER appuser
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m bot.control_plane.healthcheck
CMD ["python", "-m", "bot.control_plane.main"]
