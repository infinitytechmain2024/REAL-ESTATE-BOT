FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10007 campaign
# Discovery reuses the Facebook activity helpers under bot/services, so the
# runner needs the bot's own requirements (no browser: it drives the Browser
# Session Manager over HTTP, and facebook-runner reads the groups).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Agent Reach's X backend, in its own virtualenv so its dependencies never meet the bot's.
# Only `twitter search` is ever run (bot/campaign/xsearch.py); pinned, never tracking latest.
RUN python -m venv /opt/twitter-cli && /opt/twitter-cli/bin/pip install --no-cache-dir twitter-cli==0.8.5
ENV X_SEARCH_BINARY=/opt/twitter-cli/bin/twitter
COPY bot/ ./bot/
RUN chown -R campaign:campaign /app
USER campaign
CMD ["python", "-m", "bot.campaign.runner"]
