# Development helpers. Everything works without them -- see README.md.

PYTHON       ?= python3.11
VENV         ?= .venv
BIN          := $(VENV)/bin
SEARXNG_PORT ?= 8888

export PYTHONPATH := $(CURDIR):$(CURDIR)/searxng

.DEFAULT_GOAL := help
.PHONY: help setup install browsers run searxng check lint probe-gate check-imports \
        check-config check-sql check-api check-vendor probe-pipeline docker-up docker-down clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Ask for your keys and write .env (nothing is sent anywhere)
	@$(PYTHON) scripts/setup_env.py

test: ## Run the test suite
	$(BIN)/python -m pytest

install: ## Create the venv, install every dependency, fetch the browser
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt \
		-r requirements-dev.txt \
		-r searxng/requirements.txt \
		-r searxng/requirements-server.txt
	$(MAKE) browsers

browsers: ## Download the Chromium build Playwright drives (~150 MB)
	# Only the parser's fallback fetcher uses this one. The Facebook session
	# is deliberately a different browser -- channel="chrome", i.e. the real
	# Google Chrome you already have installed -- so nothing here fetches it.
	$(BIN)/playwright install chromium

run: ## Run the bot (expects SearXNG to be up, or `make searxng` in another shell)
	$(BIN)/python -m bot.main

searxng: ## Run the SearXNG JSON API on 127.0.0.1:$(SEARXNG_PORT)
	SEARXNG_SECRET=$${SEARXNG_SECRET:-dev-secret} \
	SEARXNG_SETTINGS_PATH=$(CURDIR)/searxng/settings/settings.yml \
	$(BIN)/granian --interface wsgi --host 127.0.0.1 --port $(SEARXNG_PORT) \
		searxng.api_only:application

llm: ## Run a local model on 127.0.0.1:8080 (llama.cpp; see DEPLOYMENT.md §11)
	./scripts/run_llm.sh

check: check-vendor lint check-imports check-config check-sql test probe-gate probe-pipeline ## Run every check

lint: ## Lint with the pinned ruff
	# No `ruff format --check` here: the tree predates the current formatter
	# and 16 files would fail it today. Reformatting them is its own commit.
	$(BIN)/ruff check .

probe-gate: ## Drive the Facebook live-view gate end to end (no browser needed)
	$(BIN)/python scripts/gate_probe.py

probe-pipeline: ## Check that a broken extra source cannot break a search
	$(BIN)/python scripts/pipeline_probe.py

check-vendor: ## Verify the vendored SearXNG snapshot is complete in git
	# Needs no venv and no dependencies -- run it with any python3.
	$(BIN)/python scripts/check_vendor.py

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
