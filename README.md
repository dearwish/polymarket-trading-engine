# Polymarket AI Agent

Python project for a Polymarket trading agent with three clearly separated layers:

- Deterministic execution built around official Polymarket APIs and `py-clob-client`
- Research and model-scoring layer using OpenRouter by default
- Operator-facing CLI and deployment model designed for safe paper trading first, then tightly gated live trading

## Current Status

The repository now includes a working paper and read-only live-readiness stack:

- Python package under `src/polymarket_ai_agent`
- operator CLI via `polymarket-ai-agent`
- settings/config loading from `.env`
- Polymarket market discovery and order book snapshot connector
- authenticated read-only Polymarket account diagnostics
- external BTC price feed connector
- research, scoring, risk, execution, and journaling engines
- paper trading and read-only simulation flows
- hard-gated live execution path
- live preflight and live order inspection commands
- SQLite and JSONL logging
- test suite covering connectors, scoring, risk, execution, service, and CLI

Important:

- this repo can authenticate against Polymarket and inspect account state
- this repo can simulate and paper-trade decisions
- this repo has a real live order-posting code path
- live posting is still disabled by default and requires explicit config and CLI confirmation

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
polymarket-ai-agent status
polymarket-ai-agent scan --limit 5
```

## Makefile Shortcuts

```bash
make bootstrap
make test
make status
make auth-check
make doctor
make live-preflight
make live-orders
make simulate-active
make simulate-market MARKET_ID=123
make simulate-loop-active ITERATIONS=3 INTERVAL=0
```

## Operator Workflow

Recommended sequence:

1. `make status`
2. `make auth-check`
3. `make doctor`
4. `make live-preflight`
5. `make live-orders`

What these do:

- `status`
  - shows runtime mode, safety state, and authenticated account summary
- `auth-check`
  - verifies wallet/private-key/funder auth and reads collateral balance
- `doctor`
  - combines auth, active market, order book, and current simulated decision
- `live-preflight`
  - evaluates whether a live trade would currently be allowed and lists blockers
- `live-orders`
  - inspects current authenticated open orders without posting anything

## Live Trading Safety Model

Live trading is intentionally hard-gated.

Real order posting requires all of:

- `TRADING_MODE=live`
- `LIVE_TRADING_ENABLED=true`
- valid Polymarket auth
- a preflight that passes risk checks
- explicit CLI confirmation via `--confirm-live`

Default safe state:

```env
TRADING_MODE=paper
LIVE_TRADING_ENABLED=false
```

## Planned Architecture

- `connectors/polymarket`
  - Gamma/Data/CLOB reads
  - token resolution
  - order book snapshots
  - account state
  - authenticated order inspection
- `connectors/external_feeds`
  - external BTC price feed snapshots
  - source adapters for market-family-specific evidence
- `engine/research`
  - source gathering
  - evidence normalization
  - citation packaging
- `engine/scoring`
  - market packet generation
  - feature extraction
  - model scoring
  - edge calculation
- `engine/risk`
  - exposure caps
  - spread/liquidity gates
  - cooldowns
  - expiry protection
  - daily loss kill switch
- `engine/execution`
  - order placement
  - cancel/replace logic
  - fill tracking
  - emergency flatten
- `engine/journal`
  - SQLite state
  - JSONL event logging
- `apps/operator`
  - `scan`
  - `analyze`
  - `paper`
  - `simulate`
  - `doctor`
  - `live-preflight`
  - `live`
  - `live-orders`
  - `live-order`
  - `close`
  - `status`
  - `report`

## Strategy Scope For V1

The first version is intentionally narrow:

- target one repetitive market family only
- current implemented focus: BTC daily threshold markets
- use OpenRouter as the default model gateway
- keep execution deterministic and mostly non-agentic
- require structured LLM output and local risk approval before any order can be placed

## Files

- [`PLAN.md`](./PLAN.md)
- [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md)
- [`.gitignore`](./.gitignore)

## Deployment Recommendation

Best default deployment path:

1. Local development and dry runs on your machine
2. Paper trading on a small always-on VPS or Fly.io machine
3. Tiny-size live trading on a single-region VPS with process supervision and SQLite backups

Recommended first production target:

- a small VPS on Hetzner, DigitalOcean, or an equivalent provider

Why:

- long-running polling/loop workers fit a VPS better than serverless
- trading loops need stable process state, local logs, and low operational complexity
- SQLite + JSONL journaling works naturally on a single-node service

Avoid for v1:

- Lambda/serverless-only deployment
- edge-worker-only deployment
- multi-region active-active deployment

Those models add failure modes and complexity before the trading logic is validated.
