FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10004 reach
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/acquisition/ ./bot/acquisition/
COPY bot/agent_reach/ ./bot/agent_reach/
# Reuse the narrow authenticated Browser Session Manager HTTP client. The
# container command below remains the Agent Reach adapter, never the collector.
COPY bot/facebook_collector/ ./bot/facebook_collector/
RUN chown -R reach:reach /app
USER reach
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m bot.agent_reach.healthcheck
CMD ["python", "-m", "bot.agent_reach.main"]
