FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 DISPLAY=:99
WORKDIR /app
# x11vnc and noVNC run only during an operator login session
# (bot/browser_session/interactive.py); nothing listens otherwise.
RUN apt-get update \
    && apt-get install --no-install-recommends -y x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10002 browseruser \
    && mkdir -p /profiles /screenshots \
    && chown -R browseruser:browseruser /app /profiles /screenshots
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/browser_session/ ./bot/browser_session/
RUN chown -R browseruser:browseruser /app /profiles /screenshots
USER browseruser
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m bot.browser_session.healthcheck
CMD ["sh", "-ec", "Xvfb :99 -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 & exec python -m bot.browser_session.main"]
