# Development helpers. Everything works without them -- see README.md.
# The standalone bot's targets (run, searxng, llm, probes, check-vendor,
# check-config, check-api) retired with it; see legacy/README.md.

PYTHON       ?= python3.11
VENV         ?= .venv
BIN          := $(VENV)/bin

export PYTHONPATH := $(CURDIR)

.DEFAULT_GOAL := help
.PHONY: help setup install test check lint check-imports check-sql docker-up docker-down update clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Ask for your keys and write .env (nothing is sent anywhere)
	@$(PYTHON) scripts/setup_env.py

test: ## Run the test suite
	$(BIN)/python -m pytest

install: ## Create the venv and install every dependency
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt -r requirements-dev.txt

check: lint check-imports check-sql test ## Run every check

lint: ## Lint with the pinned ruff
	# No `ruff format --check` here: the tree predates the current formatter
	# and 16 files would fail it today. Reformatting them is its own commit.
	$(BIN)/ruff check .

check-imports: ## Byte-compile the bot package and import every module
	# healthcheck modules probe their service at import time, so they are skipped.
	$(BIN)/python -m compileall -q bot
	$(BIN)/python -c "import importlib, pkgutil, bot; \
		[importlib.import_module(m.name) for m in pkgutil.walk_packages(bot.__path__, 'bot.') \
			if not m.name.endswith('.healthcheck')]; \
		print('all bot modules import cleanly')"

check-sql: ## Parse the migration with PostgreSQL's own grammar (needs pglast)
	@$(BIN)/python -c "import pglast" 2>/dev/null || { echo "pip install pglast to run this check"; exit 0; }
	$(BIN)/python -c "import pglast; \
		n = len(pglast.parse_sql(open('bot/services/db/migrations/001_init.sql').read())); \
		print(f'migration parses: {n} statements')"

docker-up: ## Build and run the compose stack
	docker compose up --build

docker-down: ## Stop it
	docker compose down

update: ## On the VPS: pull the code, refresh images, migrate, restart everything
	./scripts/update.sh

clean: ## Remove caches and build artefacts
	find . -path ./.venv -prune -o -name '__pycache__' -type d -print0 | xargs -0 rm -rf
	rm -rf .pytest_cache .mypy_cache .ruff_cache
