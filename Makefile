SHELL := /bin/sh

UV ?= uv
PYTHON ?= python
DOCKER_COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help sync demo smoke test test-unit test-api test-integration test-ui coverage lint format format-check typecheck security check api worker ui compose-up compose-down

help: ## Show the available developer commands.
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## Install the locked application and development dependencies.
	$(UV) lock --check
	$(UV) sync --frozen --all-groups

demo: sync ## Install dependencies, then start the API and web app together.
	$(UV) run codebase-intelligence demo

smoke: ## Verify the installed command and package entry point.
	$(UV) run codebase-intelligence --version
	$(UV) run $(PYTHON) -m codebase_intelligence --version

test: ## Run the deterministic test suite (live-provider tests are excluded).
	$(UV) run pytest -m "not live" --disable-socket --allow-unix-socket

test-unit: ## Run focused unit tests.
	$(UV) run pytest tests/unit -m "not live" --disable-socket --allow-unix-socket

test-api: ## Run focused API contract tests.
	$(UV) run pytest tests/api -m "not live" --disable-socket --allow-unix-socket

test-integration: ## Run local integration and retrieval evaluation tests.
	$(UV) run pytest tests/integration tests/eval -m "not live" --disable-socket --allow-unix-socket

test-ui: ## Run Streamlit presentation tests.
	$(UV) run pytest tests/ui -m "not live" --disable-socket --allow-unix-socket

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
	$(UV) run codebase-intelligence api

worker: ## Start the durable ingestion worker.
	$(UV) run codebase-intelligence worker

ui: ## Start Streamlit on the configured loopback address.
	$(UV) run codebase-intelligence ui

compose-up: ## Build and start the isolated local container stack.
	$(DOCKER_COMPOSE) up --build --detach

compose-down: ## Stop the stack without deleting persistent volumes.
	$(DOCKER_COMPOSE) down
