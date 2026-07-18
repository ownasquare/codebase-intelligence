SHELL := /bin/sh

UV ?= uv
PYTHON ?= python
DOCKER_COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help sync test coverage lint format format-check typecheck security check api worker ui compose-up compose-down

help: ## Show the available developer commands.
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## Install the locked application and development dependencies.
	$(UV) lock --check
	$(UV) sync --frozen --all-groups

test: ## Run the deterministic test suite (live-provider tests are excluded).
	$(UV) run pytest -m "not live" --disable-socket --allow-unix-socket

coverage: ## Run branch coverage and enforce the configured 80 percent floor.
	$(UV) run pytest -m "not live" --disable-socket --allow-unix-socket --cov=codebase_intelligence --cov-branch --cov-report=term-missing --cov-report=xml

lint: ## Run Ruff lint checks.
	$(UV) run ruff check .

format: ## Format Python sources and tests with Ruff.
	$(UV) run ruff format .

format-check: ## Verify Ruff formatting without changing files.
	$(UV) run ruff format --check .

typecheck: ## Run strict mypy checks.
	$(UV) run mypy

security: ## Run static and dependency vulnerability audits.
	$(UV) run bandit -c pyproject.toml -r src
	$(UV) run pip-audit --strict .

check: lint format-check typecheck security coverage ## Run the complete local quality gate.

api: ## Start the FastAPI service on the configured loopback address.
	$(UV) run uvicorn codebase_intelligence.api.app:app --host 127.0.0.1 --port 8000

worker: ## Start the durable ingestion worker.
	$(UV) run $(PYTHON) -m codebase_intelligence.worker

ui: ## Start Streamlit on the configured loopback address.
	$(UV) run streamlit run src/codebase_intelligence/ui/app.py --server.address 127.0.0.1 --server.port 8501

compose-up: ## Build and start the isolated local container stack.
	$(DOCKER_COMPOSE) up --build --detach

compose-down: ## Stop the stack without deleting persistent volumes.
	$(DOCKER_COMPOSE) down
