FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10003 collector
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/facebook_collector/ ./bot/facebook_collector/
RUN chown -R collector:collector /app
USER collector
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m bot.facebook_collector.healthcheck
CMD ["python", "-m", "bot.facebook_collector.main"]
