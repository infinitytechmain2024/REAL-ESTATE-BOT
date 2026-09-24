FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10006 analyst
COPY docker/requirements-analysis-pipeline.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/analysis_pipeline/ ./bot/analysis_pipeline/
RUN chown -R analyst:analyst /app
USER analyst
CMD ["python", "-m", "bot.analysis_pipeline.main"]
