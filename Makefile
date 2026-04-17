.DEFAULT_GOAL := help

PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
PIP := $(BIN)/pip
PYTEST := $(BIN)/pytest
CLI := $(BIN)/polymarket-ai-agent
ITERATIONS ?= 10
INTERVAL ?= 15

.PHONY: help venv install bootstrap reinstall bootstrap-force test status auth-check doctor live-preflight check report \
	simulate-active simulate-market simulate-loop-active simulate-loop-market \
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

live-preflight: install
	$(CLI) live-preflight --active

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

simulate-market: install guard-market-id
	$(CLI) simulate $(MARKET_ID)

simulate-loop-active: install
	$(CLI) simulate-loop --active --iterations $(ITERATIONS) --interval-seconds $(INTERVAL)

simulate-loop-market: install guard-market-id
	$(CLI) simulate-loop $(MARKET_ID) --iterations $(ITERATIONS) --interval-seconds $(INTERVAL)

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
		'  make live-preflight           Run the live-readiness preflight without posting orders' \
		'  make check                    Run test, status, and auth-check' \
		'  make report                   Run the CLI report command' \
		'  make simulate-active          Run a read-only simulation on the active market' \
		'  make simulate-market MARKET_ID=123' \
		'                               Run a read-only simulation for a specific market' \
		'  make simulate-loop-active [ITERATIONS=10] [INTERVAL=15]' \
		'                               Run repeated read-only simulation on the active market' \
		'  make simulate-loop-market MARKET_ID=123 [ITERATIONS=10] [INTERVAL=15]' \
		'                               Run repeated read-only simulation for a specific market' \
		'' \
		'Variables:' \
		'  PYTHON      Python executable to use (default: python3)' \
		'  VENV        Virtualenv directory (default: .venv)' \
		'  MARKET_ID   Required for simulate-market and simulate-loop-market' \
		'  ITERATIONS  Simulation loop iterations (default: 10)' \
		'  INTERVAL    Seconds between simulation iterations (default: 15)'
