# Polymarket AI Agent

Event-driven BTC trading agent for Polymarket's short-horizon markets (`btc_5m`, `btc_15m`, `btc_1h`, `btc_daily_threshold`). Architecturally split into three layers:

- **Deterministic quant scoring** (closed-form Black-Scholes + GBM + microstructure) — the hot path. Originally designed to route through an LLM (hence "AI"); the LLM path is still wired as an optional veto-only advisor, but the default decision path is fully deterministic and runs in ~2 ms per tick.
- **Event-driven execution** — Polymarket CLOB websocket + Binance BTC websocket, persistent per-market state, maker-first router with taker fallback, paper fills via VWAP book walk, live orders via `py-clob-client`.
- **Operator-facing CLI + FastAPI backend + React dashboard** — designed for safe paper trading first, then tightly gated live trading (requires `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, valid auth, preflight pass, and `--confirm-live`).

## Current Status

Production paper-trading system with 311 tests. Works end-to-end: WebSocket discovery → quant scoring → risk gating → paper execution → position tracking → dashboard.

- Python package under `src/polymarket_ai_agent`
- operator CLI via `polymarket-ai-agent`
- settings/config loading from `.env`
- Polymarket market discovery and order book snapshot connector (top-10 levels per side)
- authenticated read-only Polymarket account diagnostics
- external BTC price feed connector (REST + websocket)
- research, scoring, risk, execution, and journaling engines
- paper trading and read-only simulation flows
- hard-gated live execution path
- live preflight and live order inspection commands
- SQLite and JSONL logging
- **event-driven asyncio daemon** with Polymarket CLOB + Binance BTC websocket subscriptions, rolling per-market and BTC state, and a pluggable decision callback (Phase 1)
- **deterministic quant fair-value scorer** (closed-form GBM + momentum tilt + per-side edge after slippage and fees) running on every daemon tick (Phase 2)
- **maker-first / taker-fallback execution router** with VWAP paper fills, SELL-side support, live-fill → PositionRecord bridge, and a `close_position` path that posts a SELL-side counter order on Polymarket (Phase 3)
- **per-family risk profiles** (btc_1h / btc_15m / btc_5m) with tighter stale-data ceilings, a dynamic exit buffer scaled against the family's candle window, a correlation cap on net BTC directional exposure, and `max_concurrent_positions` replacing the single-position rule (Phase 4)
- **SQLite hygiene** — WAL mode, `synchronous=NORMAL`, explicit indexes on every hot lookup column, bounded `events.jsonl` tail reads, and an auto-prune loop (Phase 4; see the SQLite & Log-Growth Risk section in `docs/ROADMAP.md`)
- **operational readiness** — daemon heartbeat file, `/api/healthz` + `/api/metrics` (JSON + Prometheus), background retention / WAL-checkpoint / size-gauge maintenance loop, VACUUM INTO backups, kill-switch on auth failure + stale heartbeat, systemd units + nightly backup timer + logrotate in `docs/DEPLOYMENT.md` (Phase 5)
- **paper soak hardening** — `WS_SSL_VERIFY` setting for proxy/VPN environments; 300-second minimum TTE guard in market discovery (Polymarket's `closed=false` lags behind resolution); `btc_log_return_vs_strike` field in `EvidencePacket` so the quant scorer uses `ln(S/K)` (Black-Scholes distance-to-strike) for threshold markets instead of short-term momentum
- **soak analysis** — `scripts/analyze_soak.py` correlates `daemon_tick` journal entries against resolved market outcomes and reports hit rate, mean edge captured, abstain rate, and Brier score. `--shadow` prints a base-vs-shadow (htf_tilt) A/B table of the same metrics; `--hold-to-expiry` loads `position_closed` events and compares realized stopped P&L against the P&L the position would have earned if held until resolution, broken down by close reason
- **slug-prediction discovery** (Phase 7) — rolling `btc-updown-5m`, `btc-updown-15m`, and `bitcoin-up-or-down-<date>-<hr>{am,pm}-et` markets do **not** appear in Polymarket's bulk `/markets` or `/events` listings. For `btc_5m` / `btc_15m` / `btc_1h` the connector now predicts the next 3 window-start slugs and fetches `/events/slug/<slug>` directly. Per-family `min_tte` floor in `discover_markets` so a 5-minute market with ~30s left still gets picked up. Match scores also require the canonical slug prefix so daily "Up or Down" decoys can't sneak into the short-horizon families.
- **daemon auto paper execution** (opt-in) — set `DAEMON_AUTO_PAPER_EXECUTE=true` in `.env` and the daemon's decision callback routes every APPROVED signal through the real risk → execute → portfolio pipeline, so simulated trades accumulate in the Portfolio tab and `positions` DB table without a separate CLI runner. Open positions run through the full TP-ladder / trailing-stop / fixed-SL / force-exit / TTE-buffer ladder described in [Position Lifecycle (Paper)](#position-lifecycle-paper). Safe by default: disabled unless explicitly set.
- test suite covering connectors, scoring, risk, execution, service, CLI, state/daemon/feed modules, the execution router and VWAP fills, live fill bridging, the live close flow, per-family risk profiles, btc_15m discovery, journal retention, heartbeats, `/api/metrics`/`/api/healthz`, daemon kill-switch gating, slug-prediction discovery for 5m/15m/1h families, and daemon paper-execute lifecycle — **311 tests**

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
make daemon-smoke   # 15s smoke test of the event-driven daemon
make daemon         # run the event-driven daemon (Ctrl+C to stop)
make analyze-soak   # analyze daemon_tick journal against resolved market outcomes
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

## Strategy Scope

The current live focus is **BTC "Up or Down" candle markets** — 15-minute and 5-minute repeating binary markets that resolve on close-vs-open direction. `btc_daily_threshold` (above-$K) and `btc_1h` markets are supported by the same code path but aren't the primary soak target.

- Deterministic quant scoring (Black-Scholes GBM + microstructure imbalance) is the default decision path; the OpenRouter LLM path stays wired as an optional advisor but does not gate trades today.
- Entries are screened through a candle-window filter (`MIN_CANDLE_ELAPSED_SECONDS` / `MAX_CANDLE_ELAPSED_SECONDS`) so the daemon skips the noisy opening and closing seconds of each candle.
- Exits are paper-only today, running through a configurable ladder (TP tranches → trailing stop → fixed TP/SL → pre-expiry force-exit → TTE buffer) detailed in [Position Lifecycle (Paper)](#position-lifecycle-paper).
- Live order posting is still behind the hard gates described in [Live Trading Safety Model](#live-trading-safety-model); nothing on the candle-market path is enabled for live by default.

## Files

- [`PLAN.md`](./PLAN.md)
- [`docs/ROADMAP.md`](./docs/ROADMAP.md)
- [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md)
- [`scripts/analyze_soak.py`](./scripts/analyze_soak.py)
- [`.gitignore`](./.gitignore)

## Event-Driven Daemon (Phase 1)

The daemon replaces synchronous REST polling on the hot path with websocket-driven state:

- subscribes to Polymarket CLOB market deltas for discovered btc_1h / btc_15m / btc_5m tokens
- subscribes to Binance `aggTrade` + `bookTicker` for live BTC price, with a REST seed + fallback
- maintains in-memory `MarketState` (microprice, top-5 imbalance, trade tape, signed flow) and `BtcState` (log returns at 10s/1m/5m/15m, EWMA realized vol)
- auto-reconnects with exponential backoff on disconnect
- invokes the decision callback on every update (Phase 2 quant scorer)

Configure via `.env` (see `.env.example`):

```env
POLYMARKET_WS_MARKET_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
POLYMARKET_WS_USER_URL=wss://ws-subscriptions-clob.polymarket.com/ws/user
BTC_WS_URL=wss://stream.binance.com:9443/stream
BTC_SYMBOL=btcusdt
WS_RECONNECT_BACKOFF_SECONDS=2.0
WS_RECONNECT_BACKOFF_MAX_SECONDS=30.0
DAEMON_DISCOVERY_INTERVAL_SECONDS=60
DAEMON_DECISION_MIN_INTERVAL_SECONDS=1.0
WS_SSL_VERIFY=true  # set false if a proxy/VPN presents a self-signed cert
```

## Quant Scoring (Phase 2)

`QuantScoringEngine` ([src/polymarket_ai_agent/engine/quant_scoring.py](./src/polymarket_ai_agent/engine/quant_scoring.py)) runs on every daemon tick:

- **for `btc_1h` / up-or-down markets**: drift-less GBM over τ with short-term momentum tilt — `fair_yes = Φ(log_return_5m / σ√τ × damping)` plus top-5 imbalance nudge
- **for `btc_daily_threshold` / above-$K markets**: Black-Scholes distance-to-strike — `fair_yes = Φ(ln(S/K) / σ√τ × damping)`, where the strike is parsed from the question
- per-side edge after real cost stack: `edge_yes = fair_yes − ask_yes − slippage − fee_bps`, symmetric for NO
- confidence scales with edge magnitude and degrades when slippage is high
- expiry-risk tiers configurable via `QUANT_HIGH_EXPIRY_RISK_SECONDS` / `QUANT_MEDIUM_EXPIRY_RISK_SECONDS`
- the `ScoringEngine.OpenRouter` path is preserved but now returns the same per-side edge fields
- every tick exposes `reasons_to_abstain` / `reasons_for_trade` so the dashboard can show the exact gate that fired instead of inferring it

### Regime gates

Before picking a side, `QuantScoringEngine._regime_gate` runs four independent vetoes. The primary-cause reason is inserted at the head of `reasons_to_abstain` so downstream consumers (dashboard, `analyze_soak.py`) see the binding constraint first.

| gate | env flag | effect |
|---|---|---|
| Minimum entry price | `QUANT_MIN_ENTRY_PRICE` | Blocks trades whose side's ask is below the floor — at distressed prices the bid-ask spread alone exceeds the stop-loss width. |
| Trend-based min edge | `QUANT_TREND_FILTER_ENABLED` + `QUANT_TREND_FILTER_MIN_ABS_RETURN` | Counter-trend trades need a higher edge (`QUANT_TREND_OPPOSED_STRONG_MIN_EDGE` vs 4h, `QUANT_TREND_OPPOSED_WEAK_MIN_EDGE` vs 1h). With-trend and ranging trades are unaffected. |
| Distressed market | `QUANT_TREND_DISTRESSED_MAX_ASK` | Even with sufficient edge, blocks counter-trend buys when our side's ask is below the floor — the market has already priced in the move. |
| OFI gate | `QUANT_OFI_GATE_ENABLED` + `QUANT_OFI_GATE_MIN_ABS_FLOW` | Vetoes trades that oppose strong informed order flow (`signed_flow_5s`). |
| Volatility regime | `QUANT_VOL_REGIME_ENABLED` + `QUANT_VOL_REGIME_HIGH_THRESHOLD` / `EXTREME_THRESHOLD` | Raises the edge bar in high vol, abstains outright in extreme vol. |
| `|edge|` ceiling | `QUANT_MAX_ABS_EDGE` | Forces ABSTAIN when the chosen edge is implausibly large — empirically the worst-performing bucket on previous soaks. |

### Shadow scorer (A/B)

`QUANT_SHADOW_VARIANT=htf_tilt` runs a parallel scorer on every tick without affecting live trading. Output appears on `daemon_tick` as `shadow_fair_probability` / `shadow_suggested_side` / `shadow_edge_yes` / `shadow_edge_no`. The htf_tilt variant nudges `fair_yes` by `sign(btc_log_return_1h) × QUANT_SHADOW_HTF_TILT_STRENGTH` plus an optional session bias (`QUANT_SHADOW_SESSION_BIAS_EU` / `QUANT_SHADOW_SESSION_BIAS_US`). Compare offline with `scripts/analyze_soak.py --shadow`.

## Position Lifecycle (Paper)

With `DAEMON_AUTO_PAPER_EXECUTE=true`, every APPROVED decision opens a paper position that runs through a fixed exit priority (first match wins, re-evaluated on every tick):

1. **TP ladder** — `PAPER_TP_LADDER="0.15:0.5,0.30:0.25"` closes 50% at +15% PnL and another 25% at +30% (as a fraction of the original size).
2. **Trailing stop** — tracks the peak token price and closes when price drops `PAPER_TRAILING_STOP_PCT` below peak. Only arms once peak clears `entry × (1 + PAPER_TRAIL_ARM_PCT)`, and the trigger is floored at entry so a freshly armed trail can't fire as a realized loss. Set to `0.0` to disable.
3. **Fixed take-profit** — `PAPER_TAKE_PROFIT_PCT` (skipped after any ladder tranche fires, so it doesn't eat the runner).
4. **Fixed stop-loss** — `PAPER_STOP_LOSS_PCT` (unconditional backstop).
5. **Time-based force-exit** — `POSITION_FORCE_EXIT_TTE_SECONDS` closes the position at this TTE regardless of PnL, above the final-seconds noise band.
6. **TTE exit buffer** — the per-family dynamic buffer, last-resort close just before expiry.

Triggers evaluate against the bid price (the level we'd actually sell into), not mid, so the threshold and the realized exit live in the same frame. Closes walk the live bid book for a VWAP fill instead of nudging mid.

Safety throttles layered on top of the ladder:

- `MIN_ENTRY_TTE_SECONDS` — rejects entries too close to resolution.
- `MIN_CANDLE_ELAPSED_SECONDS` / `MAX_CANDLE_ELAPSED_SECONDS` — blocks entries in the noisy opening or closing seconds of each candle (candle-style families only).
- `PAPER_ENTRY_COOLDOWN_SECONDS` — after any close on a market, blocks re-entry on the same market for this many seconds.
- `MAX_CONSECUTIVE_LOSSES` — daemon-wide kill-switch (`consecutive_loss_limit`) after N losing closes in a row.

Every full close emits a self-contained `position_closed` journal event (market, side, size, entry, exit, pnl, hold_seconds, tte_at_close, fair_prob_at_close, edge_at_close) so downstream analysis needs no DB join.

## Per-Family Risk Profiles (Phase 4)

Risk gates now resolve per family instead of operating off a single global scalar. The active profile is derived from `settings.market_family` at `RiskEngine` construction; explicit `Settings` overrides always win, so env-tuned deployments stay backward-compatible.

| family   | stale data | exit buffer pct × window | max concurrent |
|----------|------------|--------------------------|----------------|
| btc_1h   | 5s         | 0.05 × 3600 = 180s       | 2              |
| btc_15m  | 3s         | 0.07 × 900 = 63s         | 2              |
| btc_5m   | 2s         | 0.10 × 300 = 30s         | 1              |

- `max_concurrent_positions` replaces the old single-position rule.
- `max_net_btc_exposure_usd` caps `|long_btc_usd − short_btc_usd|` across all open BTC positions — YES counts as long-BTC, NO as short-BTC.
- `AccountState` now carries `long_btc_exposure_usd`, `short_btc_exposure_usd`, `net_btc_exposure_usd`, and `total_exposure_usd`, populated by `PortfolioEngine.get_account_state`.

## SQLite & Event-Log Hygiene

The daemon is an append-heavy writer; both `data/agent.db` and `logs/events.jsonl` will grow without bound on a busy deployment. Phase 4 adds the following defaults:

- `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` + `temp_store=MEMORY` on both database files
- explicit indexes on every hot lookup column (positions.status, positions.market_id+status, positions.closed_at, order_attempts.recorded_at, order_attempts.market_id, live_orders.status, live_orders.updated_at, reports.created_at)
- `Journal.read_recent_events` uses a 64KB backwards-chunk tail-read, so peeking at the last N lines of a multi-GB JSONL no longer OOMs
- `Journal.prune_events_jsonl(max_bytes, keep_tail_bytes)` + `log_event` auto-prune every `prune_check_every` writes once the file exceeds `events_jsonl_max_bytes` (default 200 MB, keeping the last 50 MB)

See **SQLite & Log-Growth Risk Analysis** in [`docs/ROADMAP.md`](./docs/ROADMAP.md) for the full audit of write amplification, locking, WAL checkpointing, and backup concerns — plus the remaining Phase 5 items (retention for `order_attempts`, periodic `VACUUM`, off-host backup, `/api/metrics` db-size gauge).

## Operational Readiness (Phase 5)

The agent ships with the pieces needed to run unattended on a single VPS:

- **Daemon heartbeat** — every `DAEMON_HEARTBEAT_INTERVAL_SECONDS` the daemon writes `data/daemon_heartbeat.json` with its full `DaemonMetrics` (counters, latency, active markets, safety-stop reason). The operator API reads the same file to surface it in `/api/metrics` and `/api/healthz` without needing a shared process.
- **Kill-switch** — `safety_stop_reason` covers `daily_loss_limit`, `rejected_order_limit`, `consecutive_loss_limit` (N losing closes in a row, configured via `MAX_CONSECUTIVE_LOSSES`), `auth_not_ready` (live mode only), and `daemon_heartbeat_stale`. When a stop fires the daemon journals a `safety_stop` event and stops firing decision callbacks until the condition clears.
- **Maintenance loop** — separate from the decision loop, runs every `DAEMON_MAINTENANCE_INTERVAL_SECONDS` (default 1 hour). Prunes history older than `DAEMON_PRUNE_HISTORY_DAYS`, auto-prunes `events.jsonl`, runs `pragma wal_checkpoint(TRUNCATE)`, and refreshes DB / events size gauges.
- **Backups via VACUUM INTO** — `polymarket-ai-agent backup data/backups/` (or `make backup DEST=...`) produces a consistent, compacted snapshot while the daemon is still writing.
- **Metrics & health endpoints** — `GET /api/metrics` (or `?format=prometheus`) and `GET /api/healthz` return the signals an uptime monitor + Prometheus scraper need.
- **Deployment docs** — [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) has ready-to-copy systemd units for the daemon, a nightly `VACUUM INTO` + rsync timer, a logrotate config, and kill-switch alerting guidance.

Operator Makefile shortcuts:

```bash
make maintenance            # prune + WAL checkpoint
make maintenance-vacuum     # same + full VACUUM (exclusive lock)
make backup DEST=data/backups/
make heartbeat              # dump the most recent heartbeat payload
```

Prometheus scrape example:

```
GET /api/metrics?format=prometheus
# polymarket_agent_db_size_bytes                  4096
# polymarket_agent_events_jsonl_size_bytes        12345
# polymarket_agent_heartbeat_age_seconds          2.1
# polymarket_agent_open_positions                 1
# polymarket_agent_net_btc_exposure_usd           10.0
# polymarket_agent_safety_stop_triggered          0
# polymarket_agent_polymarket_events              4812
# ...
```

## Execution Router (Phase 3)

`ExecutionRouter` ([src/polymarket_ai_agent/engine/execution/router.py](./src/polymarket_ai_agent/engine/execution/router.py)) chooses between maker and taker on every approved decision:

- `GTC_MAKER` with `post_only=True` when `TTE > EXECUTION_MAKER_MIN_TTE_SECONDS` and `edge > EXECUTION_MAKER_MIN_EDGE`
- otherwise `FOK_TAKER` crossing the best opposite level
- `should_replace(...)` detects stale maker quotes for the cancel/replace loop
- paper mode fills via VWAP walk across `ask_levels` (BUY) or `bid_levels` (SELL) — no more flat-bps slippage

Live round-trips now flow through the same lifecycle as paper:

- `PolymarketConnector.execute_live_trade` honors BUY/SELL from the decision's `order_side`
- filled live orders create a `PositionRecord` via `PortfolioEngine.record_live_fill`
- `AgentService.close_position` in live mode posts a SELL-side counter order, then records the realised exit price on the fill
- `PolymarketConnector.replace_live_order` supports cancel-and-repost for drifting maker quotes

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
