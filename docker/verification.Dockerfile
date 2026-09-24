FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10007 verifier
COPY docker/requirements-verification.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/__init__.py bot/telegram_webapp.py ./bot/
COPY bot/verification/ ./bot/verification/
# The watchdog reuses the collector's Browser Session Manager client and its
# challenge detector, nothing else from the collector.
COPY bot/facebook_collector/__init__.py bot/facebook_collector/browser.py bot/facebook_collector/challenges.py ./bot/facebook_collector/
RUN chown -R verifier:verifier /app
USER verifier
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -m bot.verification.healthcheck
CMD ["python", "-m", "bot.verification.main"]
