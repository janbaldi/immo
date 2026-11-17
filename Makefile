# Docker is the default. Without Docker, add  IMMO=.venv/bin/immo  (after `make dev-install`).
IMMO ?= docker compose exec web immo

.PHONY: up down logs restart admin check-live report discover run train statbel test lint typecheck dev-install dev-web dev-worker record

up:            ## start everything (database, web app, worker, test mailbox)
	docker compose up -d --build
	@echo "Open http://localhost:8000"

down:          ## stop everything (your data is kept)
	docker compose down

logs:          ## watch what the app and the agents are doing
	docker compose logs -f web worker

restart:
	docker compose restart web worker

admin:         ## make an account admin: make admin EMAIL=you@example.com
	$(IMMO) admin create $(EMAIL)

check-live:    ## run the pipeline once against live sources + test alerts
	$(IMMO) check-live

report:        ## 14-day acceptance report (saved in reports/)
	$(IMMO) report --check-links

discover:      ## find agency websites in the province (results wait for review in Admin -> Sources)
	$(IMMO) discover

run:           ## poll all sources once now
	$(IMMO) run

train:         ## retrain the fair-value model now
	$(IMMO) train

statbel:       ## import Statbel municipality prices
	$(IMMO) statbel-import

record:        ## save a live page as a test fixture: make record URL=https://... NAME=agency-detail
	$(IMMO) record-fixture $(URL) $(NAME)

test:
	.venv/bin/pytest --cov --cov-report=term-missing

lint:
	.venv/bin/ruff check immo_agent tests && .venv/bin/ruff format --check immo_agent tests

typecheck:
	.venv/bin/mypy immo_agent

dev-install:   ## local Python setup without Docker (uses SQLite)
	uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev,llm]"

dev-web:
	.venv/bin/immo init && .venv/bin/immo web

dev-worker:
	.venv/bin/immo worker
