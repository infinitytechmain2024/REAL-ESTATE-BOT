FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Voice notes are sent to OpenRouter as-is, so there is no ffmpeg, no local
# speech model, no model volume and no GPU. The process has no browser
# tooling and no host-facing port.
RUN useradd --create-home --uid 10001 appuser
COPY docker/requirements-telegram-control.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/__init__.py ./bot/__init__.py
COPY bot/control_plane/ ./bot/control_plane/
COPY bot/orchestra/ ./bot/orchestra/
RUN chown -R appuser:appuser /app
USER appuser
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m bot.control_plane.healthcheck
CMD ["python", "-m", "bot.control_plane.main"]
