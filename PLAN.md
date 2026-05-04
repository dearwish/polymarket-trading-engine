# Polymarket Trading Engine Plan (Revised With Repo Foundations)

## Summary

Build a new Python-based Polymarket trading agent with three separated layers:

- Execution layer modeled after `Polymarket/poly-market-maker` and built on the official `py-clob-client`
- Research and agent layer optionally wrapped by `HKUDS/Vibe-Trading` or NanoBot/OpenClaw for multi-agent research, OpenRouter routing, and operator workflows
- Strategy layer for Polymarket-specific source analysis, market scoring, risk gating, and order decisions

What to use from the reviewed repos:

- Adopt as execution reference: `Polymarket/poly-market-maker`
- Adopt as orchestration and reference layer: `HKUDS/Vibe-Trading`
- Reference only, not base: `solcanine/openclaw-ai-polymarket-trading-bot`
- Reference only, not base: `ImMike/polymarket-arbitrage`
- Do not use as foundation: `moon-dev-ai-agents`

The first version should target one narrow Polymarket market family and use OpenRouter-first model access.

## Base Architecture

Implement the repo with these subsystems:

- `connectors/polymarket`
  - Gamma/Data/CLOB reads
  - market metadata
  - token resolution
  - order book snapshots
  - account state
- `connectors/external_feeds`
  - external truth feeds for the chosen market family
- `engine/research`
  - source gathering
  - normalization
  - evidence extraction
  - citation packaging
- `engine/scoring`
  - feature generation
  - market-vs-model edge calculation
  - abstain logic
- `engine/risk`
  - max size
  - max exposure
  - min liquidity
  - max spread
  - cooldown
  - expiry buffer
  - daily loss cap
- `engine/execution`
  - order creation
  - replace/cancel
  - fill tracking
  - emergency flatten
  - retries
- `engine/journal`
  - SQLite for normalized state
  - JSONL logs for raw market snapshots, evidence packets, model I/O, and execution events
- `apps/operator`
  - CLI commands for `scan`, `analyze`, `paper`, `live`, `close`, `status`, and `report`

## Repo Usage Rules

Use `Polymarket/poly-market-maker` for:

- authenticated CLOB interaction patterns
- periodic sync loop design
- open-order reconciliation
- safe cancel/place lifecycle
- per-market execution discipline

Use `HKUDS/Vibe-Trading` or NanoBot/OpenClaw for:

- OpenRouter-backed LLM orchestration
- multi-agent research delegation
- operator chat or MCP integration
- reusable research workflows, not trading execution

Use `solcanine/openclaw-ai-polymarket-trading-bot` only for:

- examples of short-horizon Polymarket market selection
- optional heuristic features like EMA/RSI/whale-flow inputs
- comparison against the custom implementation

Use `ImMike/polymarket-arbitrage` only for:

- ideas on dashboarding
- risk-manager boundaries
- signal/execution separation
- not for live Polymarket auth or final execution logic

Do not rely on `moon-dev-ai-agents` for Polymarket execution or strategy.

## Strategy Design For V1

The first strategy should be decision-simple and operationally strict.

Recommended v1:

- target BTC 5-minute Up/Down or one equally repetitive binary market class
- build a market packet every 10-15 seconds with:
  - current implied probabilities
  - best bid/ask, midpoint, spread, and depth
  - time to expiry
  - recent trade flow and short-horizon movement
  - external BTC price feed snapshot
  - market metadata and resolution text
- generate an `EvidencePacket`
- send only structured evidence to the model
- require strict JSON output:
  - `fair_probability`
  - `confidence`
  - `reasons_for_trade`
  - `reasons_to_abstain`
  - `expiry_risk`
  - `suggested_side`
- compute edge locally:
  - `edge = model_fair_probability - market_implied_probability`
- allow trade only if all gates pass:
  - min confidence
  - min edge after fees and slippage assumptions
  - max spread
  - min depth and liquidity
  - not near expiry
  - exposure limits not breached

The LLM proposes belief. The engine decides tradeability.

## Execution Policy

Execution must be deterministic and mostly non-agentic.

Rules:

- one position per market
- no averaging down
- no martingale
- no multi-leg portfolio logic in v1
- default entry via FOK or tightly controlled aggressive orders
- timed exit or pre-expiry forced exit
- emergency kill switch for:
  - API auth failures
  - repeated rejected orders
  - stale data
  - daily loss limit breach

The execution engine should be coded independently of the agent framework so it can run unattended without chat-loop dependence.

## Agent Orchestration Integration

If using Vibe-Trading, NanoBot, or OpenClaw, keep them outside the execution-critical path.

Agent responsibilities:

- gather source pages and summarize them
- compare contradictory sources
- explain why a market should be skipped
- produce operator reports
- run post-trade review and strategy diagnostics

Non-agent deterministic core:

- market discovery
- feature computation
- risk checks
- order placement
- fill handling
- persistence

This separation avoids model or orchestration failures directly causing bad orders.

## Public Interfaces

Define these interfaces up front:

- `discover_markets() -> list[MarketCandidate]`
- `build_evidence_packet(market_id) -> EvidencePacket`
- `score_market(packet) -> MarketAssessment`
- `decide_trade(assessment, account_state) -> TradeDecision`
- `execute_trade(decision) -> ExecutionResult`
- `manage_open_positions() -> list[PositionAction]`
- `generate_operator_report(session_id) -> Report`

Core types:

- `MarketCandidate`
- `MarketSnapshot`
- `EvidencePacket`
- `MarketAssessment`
- `TradeDecision`
- `ExecutionResult`
- `PositionRecord`
- `RiskState`

Persistence:

- SQLite for normalized state
- JSONL for raw prompts, model output, market snapshots, and execution logs
- `.env` for secrets and runtime config

## Test Plan

Required tests:

- Polymarket market discovery returns valid active markets and token IDs for the chosen niche
- CLOB auth works with create/derive creds and fails safely on invalid config
- evidence builder handles missing descriptions, missing linked sources, and stale feeds
- model output parsing rejects malformed or non-schema JSON
- risk engine blocks low-edge, low-liquidity, near-expiry, and overexposure trades
- execution engine handles rejects, partial fills, cancel/replace flow, and repeated retries safely
- journaling records exact causal chain from snapshot to fill
- paper mode replays decisions and computes realized PnL correctly
- end-to-end dry run can scan, analyze, abstain, and execute simulated trades without crashes

Acceptance criteria:

- read-only market scan runs stably for a full session
- paper trading shows tracked hit rate, realized edge, slippage, and abstain rate
- tiny-size live mode respects all caps and cleanly recovers from API or network faults

## Assumptions

- OpenRouter is the default LLM gateway using an OpenAI-compatible client
- Python is the primary implementation language
- `poly-market-maker` is treated as the main execution reference, not copied blindly
- `Vibe-Trading` or NanoBot/OpenClaw are optional wrappers for research and operator UX, not the execution kernel
- the first version trades a single repeatable market family before any multi-market expansion
- no reviewed repo is a trustworthy plug-and-play earning bot
- positive expected value is the target, not guaranteed profitability
