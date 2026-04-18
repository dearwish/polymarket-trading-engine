.DEFAULT_GOAL := help

PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
PIP := $(BIN)/pip
PYTEST := $(BIN)/pytest
CLI := $(BIN)/polymarket-ai-agent
ITERATIONS ?= 10
INTERVAL ?= 15

.PHONY: help venv install bootstrap reinstall bootstrap-force test status auth-check doctor api-dev web-install web-dev web-build live-preflight live-activity live-orders tracked-live-orders refresh-live-orders live-reconcile live-watch live-trades live-cancel check report \
	simulate-active simulate-market simulate-loop-active simulate-loop-market \
	daemon daemon-smoke \
	analyze-soak \
	guard-market-id

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

$(VENV)/.installed: $(BIN)/python pyproject.toml
	$(PIP) install -e ".[dev]"
	@touch $(VENV)/.installed

venv: $(BIN)/python

install: $(VENV)/.installed

bootstrap: install

reinstall: $(BIN)/python
	$(PIP) install -e ".[dev]"
	@touch $(VENV)/.installed

bootstrap-force: reinstall

test: install
	$(PYTEST)

status: install
	$(CLI) status

auth-check: install
	$(CLI) auth-check

doctor: install
	$(CLI) doctor --active

api-dev: install
	$(BIN)/uvicorn polymarket_ai_agent.apps.api.main:app --host 127.0.0.1 --port 8000 --reload

web-install:
	cd web && npm install

web-dev:
	cd web && npm run dev

web-build:
	cd web && npm run build

live-preflight: install
	$(CLI) live-preflight --active

live-activity: install
	$(CLI) live-activity --active

live-orders: install
	$(CLI) live-orders

tracked-live-orders: install
	$(CLI) tracked-live-orders

refresh-live-orders: install
	$(CLI) refresh-live-orders

live-reconcile: install
	$(CLI) live-reconcile --active

live-watch: install
	$(CLI) live-watch --active --iterations $(ITERATIONS) --interval-seconds $(INTERVAL)

live-trades: install
	$(CLI) live-trades

live-cancel: install guard-order-id
	$(CLI) live-cancel $(ORDER_ID) --confirm-cancel

check: test status auth-check

report: install
	$(CLI) report

simulate-active: install
	$(CLI) simulate --active

guard-market-id:
	@if [ -z "$(MARKET_ID)" ]; then \
		echo "MARKET_ID is required. Usage: make simulate-market MARKET_ID=123"; \
		exit 1; \
	fi

guard-order-id:
	@if [ -z "$(ORDER_ID)" ]; then \
		echo "ORDER_ID is required. Usage: make live-cancel ORDER_ID=abc123"; \
		exit 1; \
	fi

simulate-market: install guard-market-id
	$(CLI) simulate $(MARKET_ID)

simulate-loop-active: install
	$(CLI) simulate-loop --active --iterations $(ITERATIONS) --interval-seconds $(INTERVAL)

simulate-loop-market: install guard-market-id
	$(CLI) simulate-loop $(MARKET_ID) --iterations $(ITERATIONS) --interval-seconds $(INTERVAL)

daemon: install
	$(CLI) daemon

daemon-smoke: install
	$(CLI) daemon --duration-seconds 15

maintenance: install
	$(CLI) maintenance

maintenance-vacuum: install
	$(CLI) maintenance --vacuum

backup: install
	@if [ -z "$(DEST)" ]; then \
		echo "DEST is required. Usage: make backup DEST=data/backups/agent.db"; \
		exit 1; \
	fi
	$(CLI) backup $(DEST)

heartbeat: install
	$(CLI) heartbeat

analyze-soak: install
	$(BIN)/python scripts/analyze_soak.py

help:
	@printf '%s\n' \
		'Available targets:' \
		'  make venv                     Create the local virtual environment' \
		'  make install                  Install the package and dev dependencies' \
		'  make bootstrap                Create the venv and install dependencies' \
		'  make reinstall                Force reinstall the package and dev dependencies' \
		'  make bootstrap-force          Force bootstrap to reinstall dependencies' \
		'  make test                     Run the full pytest suite' \
		'  make status                   Run the CLI status command' \
		'  make auth-check               Run the CLI auth-check command' \
		'  make doctor                   Run the combined read-only account/market/simulation diagnostic' \
		'  make api-dev                  Run the FastAPI operator backend on http://127.0.0.1:8000' \
		'  make web-install              Install the React dashboard dependencies' \
		'  make web-dev                  Run the React dashboard on http://127.0.0.1:5180' \
		'  make web-build                Build the React dashboard for production' \
		'  make live-preflight           Run the live-readiness preflight without posting orders' \
		'  make live-activity            Run the top-level live activity snapshot (preflight, orders, trades)' \
		'  make live-orders              Inspect authenticated open live orders without posting new ones' \
		'  make tracked-live-orders      Inspect locally tracked submitted live orders' \
		'  make refresh-live-orders      Refresh tracked live orders against Polymarket' \
		'  make live-reconcile           Refresh tracked orders and recent trades in one runbook step' \
		'  make live-watch [ITERATIONS=10] [INTERVAL=15]' \
		'                               Repeatedly run live reconciliation in read-only mode' \
		'  make live-trades              Inspect recent authenticated trade history without posting orders' \
		'  make live-cancel ORDER_ID=abc123' \
		'                               Cancel a specific live order with explicit confirmation' \
		'  make check                    Run test, status, and auth-check' \
		'  make report                   Run the CLI report command' \
		'  make simulate-active          Run a read-only simulation on the active market' \
		'  make simulate-market MARKET_ID=123' \
		'                               Run a read-only simulation for a specific market' \
		'  make simulate-loop-active [ITERATIONS=10] [INTERVAL=15]' \
		'                               Run repeated read-only simulation on the active market' \
		'  make simulate-loop-market MARKET_ID=123 [ITERATIONS=10] [INTERVAL=15]' \
		'                               Run repeated read-only simulation for a specific market' \
		'  make daemon                   Run the event-driven market-data daemon (Phase 1)' \
		'  make daemon-smoke             Run the daemon for 15s to smoke-test websocket plumbing' \
	'  make analyze-soak             Analyze daemon_tick journal against resolved market outcomes' \
		'' \
		'Variables:' \
		'  PYTHON      Python executable to use (default: python3)' \
		'  VENV        Virtualenv directory (default: .venv)' \
		'  MARKET_ID   Required for simulate-market and simulate-loop-market' \
		'  ORDER_ID    Required for live-cancel' \
		'  ITERATIONS  Simulation loop iterations (default: 10)' \
		'  INTERVAL    Seconds between simulation iterations (default: 15)'
