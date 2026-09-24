FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10005 scrapling
COPY docker/requirements-scrapling-connector.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/acquisition/ ./bot/acquisition/
COPY bot/scrapling_connector/ ./bot/scrapling_connector/
RUN chown -R scrapling:scrapling /app
USER scrapling
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m bot.scrapling_connector.healthcheck
CMD ["python", "-m", "bot.scrapling_connector.main"]
