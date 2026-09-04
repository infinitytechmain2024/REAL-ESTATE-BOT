# Development helpers. Everything works without them -- see README.md.

PYTHON       ?= python3.11
VENV         ?= .venv
BIN          := $(VENV)/bin
SEARXNG_PORT ?= 8888

export PYTHONPATH := $(CURDIR):$(CURDIR)/searxng

.DEFAULT_GOAL := help
.PHONY: help install install-dev run searxng check check-imports check-config check-sql check-api test lint docker-up docker-down clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install bot + SearXNG dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt \
		-r searxng/requirements.txt \
		-r searxng/requirements-server.txt

install-dev: ## Install the test and lint tooling on top of `install`
	$(BIN)/pip install -r requirements-dev.txt

run: ## Run the bot (expects SearXNG to be up, or `make searxng` in another shell)
	$(BIN)/python -m bot.main

searxng: ## Run the SearXNG JSON API on 127.0.0.1:$(SEARXNG_PORT)
	SEARXNG_SECRET=$${SEARXNG_SECRET:-dev-secret} \
	SEARXNG_SETTINGS_PATH=$(CURDIR)/searxng/settings/settings.yml \
	$(BIN)/granian --interface wsgi --host 127.0.0.1 --port $(SEARXNG_PORT) \
		searxng.api_only:application

check: lint check-imports check-config check-sql test ## Run every check

lint: ## Lint the bot package
	$(BIN)/ruff check bot tests

test: ## Run the unit tests
	$(BIN)/pytest

check-imports: ## Byte-compile the bot package and import every module
	$(BIN)/python -m compileall -q bot
	$(BIN)/python -c "import importlib, pkgutil, bot; \
		[importlib.import_module(m.name) for m in pkgutil.walk_packages(bot.__path__, 'bot.')]; \
		print('all bot modules import cleanly')"

check-config: ## Validate that .env.example loads through the real settings
	$(BIN)/python -c "from dotenv import load_dotenv; load_dotenv('.env.example'); \
		from bot.config import Settings; s = Settings(); \
		print('config OK:', s.llm.provider, s.stt.provider, s.searxng.url)"

check-sql: ## Parse the migrations with PostgreSQL's own grammar (needs pglast)
	@$(BIN)/python -c "import pglast" 2>/dev/null || { echo "pip install pglast to run this check"; exit 0; }
	$(BIN)/python -c "import glob, pglast; \
		[print(f'{f}: {len(pglast.parse_sql(open(f).read()))} statements') \
		 for f in sorted(glob.glob('bot/services/db/migrations/*.sql'))]"

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
