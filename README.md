# Polymarket AI Agent

Python project scaffold for a Polymarket trading agent with three clearly separated layers:

- Deterministic execution built around official Polymarket APIs and `py-clob-client`
- Research and model-scoring layer using OpenRouter by default
- Operator-facing CLI and deployment model designed for safe paper trading first, then tightly gated live trading

## Current Status

This repository currently contains:

- the approved implementation plan
- deployment guidance
- repository hygiene files

The actual trading engine implementation is intentionally not included yet. The next step is to review this repo structure and plan, then approve iteration on the codebase.

## Implemented In This Iteration

The repository now includes the first code implementation slice:

- Python package scaffold under `src/polymarket_ai_agent`
- operator CLI via `polymarket-ai-agent`
- settings/config loading from `.env`
- Polymarket market discovery and order book snapshot connector
- external BTC price feed connector
- research, scoring, risk, execution, and journaling engines
- paper-first execution path
- SQLite and JSONL logging
- initial unit tests for risk gating

This is still not a live trading bot. The live execution path remains intentionally disabled in the scaffold while the paper path and interfaces are hardened.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
polymarket-ai-agent status
polymarket-ai-agent scan --limit 5
```

## Planned Architecture

- `connectors/polymarket`
  - Gamma/Data/CLOB reads
  - token resolution
  - order book snapshots
  - account state
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
  - `live`
  - `close`
  - `status`
  - `report`

## Strategy Scope For V1

The first version is intentionally narrow:

- target one repetitive market family only
- preferred first target: BTC 5-minute Up/Down
- use OpenRouter as the default model gateway
- keep execution deterministic and mostly non-agentic
- require structured LLM output and local risk approval before any order can be placed

## Files

- [`PLAN.md`](/Users/davidro/playground/polymarket-ai-agent/PLAN.md)
- [`docs/DEPLOYMENT.md`](/Users/davidro/playground/polymarket-ai-agent/docs/DEPLOYMENT.md)
- [`.gitignore`](/Users/davidro/playground/polymarket-ai-agent/.gitignore)

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
