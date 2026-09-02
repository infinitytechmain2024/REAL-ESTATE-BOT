# Development helpers. Everything works without them -- see README.md.

# The venv wants 3.11+, but `setup` only needs whatever python3 is on PATH:
# scripts/setup_env.py has no dependencies and runs on the system interpreter.
# macOS in particular has no bare `python`, so never assume one.
PYTHON       ?= $(shell command -v python3.11 2>/dev/null || command -v python3 2>/dev/null || echo python3)
VENV         ?= .venv
BIN          := $(VENV)/bin
SEARXNG_PORT ?= 8888

export PYTHONPATH := $(CURDIR):$(CURDIR)/searxng

.DEFAULT_GOAL := help
.PHONY: help setup env-manual models install run searxng check check-imports check-config check-sql check-api docker-up docker-down clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Ask for your keys and write .env (nothing is sent anywhere)
	@$(PYTHON) scripts/setup_env.py

env-manual: ## Print a minimal .env template to fill in by hand
	@printf '%s\n' \
		'TELEGRAM_TOKEN=' \
		'LLM_PROVIDER=openrouter' \
		'LLM_MODEL=openai/gpt-4o-mini' \
		'OPENROUTER_API_KEY=' \
		'STT_ENABLED=false'
	@echo ''
	@echo '# Скопируйте в файл .env в корне проекта и заполните два пустых значения.' 

models: ## Measure which models can handle the ranking call (see scripts/check_model.py)
	@$(PYTHON) scripts/check_model.py $(ARGS)

install: ## Create the venv and install bot + SearXNG dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt \
		-r searxng/requirements.txt \
		-r searxng/requirements-server.txt

run: ## Run the bot (expects SearXNG to be up, or `make searxng` in another shell)
	$(BIN)/python -m bot.main

searxng: ## Run the SearXNG JSON API on 127.0.0.1:$(SEARXNG_PORT)
	SEARXNG_SECRET=$${SEARXNG_SECRET:-dev-secret} \
	SEARXNG_SETTINGS_PATH=$(CURDIR)/searxng/settings/settings.yml \
	$(BIN)/granian --interface wsgi --host 127.0.0.1 --port $(SEARXNG_PORT) \
		searxng.api_only:application

check: check-imports check-config check-sql ## Run every static check

check-imports: ## Byte-compile the bot package and import every module
	$(BIN)/python -m compileall -q bot
	$(BIN)/python -c "import importlib, pkgutil, bot; \
		[importlib.import_module(m.name) for m in pkgutil.walk_packages(bot.__path__, 'bot.')]; \
		print('all bot modules import cleanly')"

check-config: ## Validate that .env.example loads through the real settings
	$(BIN)/python -c "from dotenv import load_dotenv; load_dotenv('.env.example'); \
		from bot.config import Settings; s = Settings(); \
		print('config OK:', s.llm.provider, s.stt.provider, s.searxng.url)"

check-sql: ## Parse the migration with PostgreSQL's own grammar (needs pglast)
	@$(BIN)/python -c "import pglast" 2>/dev/null || { echo "pip install pglast to run this check"; exit 0; }
	$(BIN)/python -c "import pglast; \
		n = len(pglast.parse_sql(open('bot/services/db/migrations/001_init.sql').read())); \
		print(f'migration parses: {n} statements')"

check-api: ## Smoke-test the SearXNG JSON API (SearXNG must be running)
	@curl -fsS "http://127.0.0.1:$(SEARXNG_PORT)/healthz" >/dev/null && echo "healthz OK"
	@test "$$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$(SEARXNG_PORT)/)" = "404" \
		&& echo "web UI blocked OK" || { echo "web UI is reachable"; exit 1; }
	@curl -fsS "http://127.0.0.1:$(SEARXNG_PORT)/search?q=test&format=json" >/dev/null && echo "JSON search OK"

docker-up: ## Build and run the single-container setup, as on Render
	docker compose up --build

docker-down: ## Stop it
	docker compose down

clean: ## Remove caches and build artefacts
	find . -path ./searxng -prune -o -name '__pycache__' -type d -print0 | xargs -0 rm -rf
	rm -rf .pytest_cache .mypy_cache .ruff_cache
