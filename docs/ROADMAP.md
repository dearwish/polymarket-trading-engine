# Optimizing the Polymarket AI Agent for Real btc_1h / btc_15m / btc_5m Trading

## Context

The repo is a clean Codex-generated scaffold (Python + FastAPI + React) that targets short-horizon BTC directional markets on Polymarket. Architecture is well-separated (connectors / engine / service / apps) and the safety gating around live trading is thoughtful. However, the **hot path is synchronous REST + LLM polling**, the websocket client exists but is dormant, and the "fair probability" estimator is a toy heuristic or a 30-second LLM call. For btc_1h the agent can technically function but edges will be stale; for btc_15m it will trade with a consistent latency handicap; for btc_5m the current design cannot trade competitively at all.

Scope:
- Drop LLM from the hot path; deterministic quant model is primary.
- Optimize **btc_1h, btc_15m, and btc_5m in parallel**, parameterized by timeframe.
- **Maker-first with taker fallback** execution.

Note on btc_15m: the existing code ships family scorers for `btc_1h`, `btc_5m`, and `btc_daily_threshold` only (see `connectors/polymarket.py` and `config.py`). A new `btc_15m` family needs to be added with its own keyword matcher, `_active_market_max_expiry_seconds` (suggested: 30 min window), `_discovery_request_limit`, and `RiskProfile`. This is a parallel of the existing 1h/5m code paths and is folded into the phases below.

---

## What the Repo Gets Right (keep)

- Clear module boundaries (`src/polymarket_ai_agent/` — connectors, engine, service, apps).
- Strong test coverage on paper paths (`tests/`).
- Hard-gated live mode: `TRADING_MODE=live` + `LIVE_TRADING_ENABLED=true` + `--confirm-live` + preflight blockers.
- Rich runtime settings surface (`config.py`) with editable overrides — trivial to extend for per-family knobs.
- SQLite + JSONL journaling for replayable decisions.
- Market-family scoring in `connectors/polymarket.py` is a reasonable first-pass filter.
- Preflight + doctor + live-orders commands form a usable operator toolkit.

---

## Critical Gaps For Real Short-Horizon Trading

### G1. Hot path is synchronous REST polling
- `service.build_market_snapshot` makes three blocking HTTP calls (Gamma market, CLOB book, Binance) every cycle.
- CLI `run-loop` uses `time.sleep(interval)` between iterations — no event-driven behavior.
- `polymarket_ws.py` explicitly says *"intentionally not wired into the live trading path yet"*.
- Impact: at btc_5m scale, the mid can move a full cent between cycle start and order post.

### G2. LLM is a 30-second blocking call on the critical path
- `scoring.py` sets `httpx.Client(timeout=30)` and POSTs to OpenRouter synchronously on every `analyze_market`.
- No caching, no streaming, no async.

### G3. Fair-value model is a toy
- Heuristic fallback adds `±1.5%` based on sign of "recent price change" plus a constant `+1%`.
- LLM prompt sends a thin JSON blob; without market-microstructure features an LLM is guessing.

### G4. Edge formula ignores fill costs
- `edge = fair - packet.market_probability` uses the stale Gamma `outcomePrices[0]`, not the ask you actually cross.
- Correct YES taker edge: `fair − ask − slippage − fees`; NO taker edge: `(1 − fair) − ask_NO − slippage`.

### G5. "Recent price change bps" is not a price change
- `recent_price_change_bps=(orderbook.midpoint − candidate.implied_probability) * 10_000` measures API staleness, not momentum.
- `recent_trade_count=0` is hardcoded; the tape is never consulted.

### G6. Execution is buy-only, no SELL, no cancel/replace
- `PolymarketConnector.execute_live_trade` always uses `BUY`. Exits cannot be posted as resting sells. No cancel/replace loop.
- `manage_open_positions()` returns `[]`.
- Paper fill always uses `orderbook.ask` regardless of side — wrong for NO-side paper trades.

### G7. Live fills never become positions
- Only `FILLED_PAPER` creates a `PositionRecord`. Live orders go to `live_orders` but never become positions → no live TTL exits, PnL, or manage.

### G8. Risk gates are blunt
- Single `open_positions >= 1` rejection prevents multi-market exposure.
- `min_edge=0.03` / `max_spread=0.04` / `min_depth_usd=200` apply globally; they should be per-family.
- `exit_buffer_seconds=5` is fixed; should be `max(floor, pct * TTE)`.
- `stale_data_seconds=30` is too loose for 5m/15m.

### G9. External feed: single REST source, high latency
- Binance `/ticker/price` over HTTP every cycle. No candle history, no realized vol, no fallback.

### G10. Paper slippage is too generous
- 10 bps × ask on a 0.5 probability = 0.0005. Real Polymarket taker slippage on 5m markets is 1–3¢.

### G11. No continuous daemon, no monitoring
- `run-loop` is a bounded for-loop; no systemd unit, health endpoint, or metrics.

### G12. API is readonly, dashboard polls
- SSE endpoints internally re-call sync REST. Dashboard polls.

---

## Target Architecture (deterministic quant core, LLM optional)

```
┌─────────────────────────────────────────────────────────────────┐
│                        asyncio daemon                           │
│  ┌──────────────┐  ┌──────────────┐   ┌──────────────────────┐  │
│  │ PolyMkt WS   │  │  BTC WS      │   │  Market-family        │  │
│  │ (book/trade) │  │ (bookTicker  │   │  discovery (60s REST) │  │
│  │              │  │  + aggTrade) │   │                       │  │
│  └──────┬───────┘  └──────┬───────┘   └──────────┬────────────┘  │
│         ▼                 ▼                      ▼                │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │               MarketState (in-memory, per market)           │ │
│  │  book, microprice, imbalance, trade tape, BTC price/vol,    │ │
│  │  TTE, candle-open price, time-elapsed-in-candle             │ │
│  └──────────────────────────────┬──────────────────────────────┘ │
│                                 ▼                                 │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │         QuantScoringEngine (deterministic, <1ms)            │ │
│  │  fair = BS-like P(BTC_T > strike | S_now, σ, τ, drift)      │ │
│  │  or fair = logistic(features) for up/down markets           │ │
│  │  edge_yes = fair - ask_yes - fees - slippage_estimate       │ │
│  │  edge_no  = (1-fair) - ask_no - fees - slippage_estimate    │ │
│  └──────────────────────────────┬──────────────────────────────┘ │
│                                 ▼                                 │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │   RiskEngine (per-family gates) → TradeDecision              │ │
│  └──────────────────────────────┬──────────────────────────────┘ │
│                                 ▼                                 │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  ExecutionEngine: router                                    │ │
│  │   TTE > T_maker_min AND edge > E_maker → GTC post-only      │ │
│  │   else                                 → FOK taker          │ │
│  │   cancel/replace on mid drift or edge change                │ │
│  │   force-close at exit_buffer = max(floor, pct * TTE)        │ │
│  └──────────────────────────────┬──────────────────────────────┘ │
│                                 ▼                                 │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Portfolio + Journal: live fills create PositionRecords;    │ │
│  │  user_orders WS reconciles status; TTL / vol-based exits   │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  LLM runs out-of-band: news / halt detection / "should we skip   │
│  this hour?" advisor, never on the tick path.                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Phased Roadmap

Each phase is independently shippable and leaves paper mode working.

**Status:** Phases 1, 2, 3, and 4 have landed on `main`. Phase 5 (operational readiness: daemon, metrics, reconciliation, chaos tests) is next.

### Phase 1 — Event-driven market plumbing (foundation) ✅
Goal: replace REST polling on the hot path with websocket-driven state.

- Wire `PolymarketMarketStream` (`connectors/polymarket_ws.py`) into the daemon. Also subscribe to the `user` channel for fills/cancels.
- Add `connectors/binance_ws.py`: aggTrade + bookTicker over websocket; fallback to REST.
- Introduce `engine/market_state.py`: per-market rolling state updated by WS events. Owns order book, trade tape (last N), computed features (microprice, top-5 imbalance, signed flow, last-trade-age).
- Introduce `engine/btc_state.py`: rolling 1-second price bars (last 120), realized vol (EWMA), log returns at 10s/1m/5m/15m.
- Add `apps/daemon/run.py` (new): asyncio entry point, discovers markets every 60s, subscribes to WS for matching token IDs, fires strategy on each event.
- Latency metrics: WS → decision, decision → order-post, exposed via `/api/metrics`.

New: `connectors/binance_ws.py`, `engine/market_state.py`, `engine/btc_state.py`, `apps/daemon/run.py`.
Modified: `connectors/polymarket_ws.py`, `service.py`, `config.py` (adds `btc_ws_url`, `polymarket_ws_user_url`, `ws_reconnect_backoff_seconds`).

Verification:
- Unit: WS reconnect logic, parse of Polymarket `book`/`last_trade_price`/`price_change`, Binance `bookTicker`+`aggTrade`, vol/return aggregators.
- Integration: 10-minute paper run in btc_1h should log ≥200 book updates/min with no REST polls on the hot path.

### Phase 2 — Deterministic quant fair-value model ✅
Goal: replace the heuristic / LLM scorer with a closed-form probability model.

- Up/down: drift-less GBM over τ, `fair_yes = 1 − Φ(-d)` with small momentum tilt from last-5m log return and signed-flow imbalance.
- Threshold: BS-style `P(S_T > K) = Φ((ln(S/K) + (μ − σ²/2)τ) / (σ√τ))` with σ from 30-min realized vol.
- New edge: `edge_side = fair_side − ask_side − take_slippage(size, book) − fee_bps` per side.
- LLM becomes an optional *veto-only* advisor with `asyncio.wait_for(..., 2.0)`.

New: `engine/quant_scoring.py`.
Modified: `engine/scoring.py` → async `LLMAdvisor`; `types.EvidencePacket` gains ask_yes/ask_no/bid_yes/bid_no/microprice/imbalance_top5/signed_flow_5s/btc_log_return_5m/btc_log_return_15m/realized_vol_30m/time_elapsed_in_candle_s; `engine/research.py` populates new fields from MarketState+BtcState.

Verification:
- Golden tests over fixtures.
- Walk-forward backtest on journaled data: hit rate > 50%, avg edge > fees.

### Phase 3 — Execution: maker-first router with cancel/replace + SELL + TTL exits ✅
- Split execution into `engine/execution/` package with `router.py` + `engine.py`.
- Route: `TTE > EXECUTION_MAKER_MIN_TTE_SECONDS` AND `edge > EXECUTION_MAKER_MIN_EDGE` → GTC post-only; else FOK taker.
- Cancel/replace helper: `ExecutionRouter.should_replace` + `PolymarketConnector.replace_live_order`.
- Fixed buy-only executor: `TradeDecision.order_side` (BUY/SELL) threaded through to `py-clob-client`.
- Live-fill → PositionRecord bridge: `record_execution` handles real live fills, and `PortfolioEngine.record_live_fill` accepts user-channel updates for orders that rest first and fill later.
- `AgentService.close_position` posts a SELL-side counter order when in live mode, gated on readonly-ready auth.
- Paper fills walk top-10 `bid_levels`/`ask_levels` VWAP-style; `ExecutionResult` reports filled/remaining shares.
- Future Phase 3 extensions (force-close near exit, stream-driven user WS reconciliation loop) are tracked alongside Phase 5 operational work.

### Phase 4 — Per-family risk + correlation-aware portfolio ✅
- `RiskProfile` dataclass + `FAMILY_PROFILE_OVERRIDES` ship per-family defaults; `resolve_risk_profile` lets explicit operator globals win so env-tuned deployments are unchanged.
- Single-position rule replaced with `max_concurrent_positions` plus a correlation cap on projected net BTC directional exposure (`max_net_btc_exposure_usd`).
- Dynamic buffer: `max(exit_buffer_seconds, exit_buffer_pct_of_tte * family_window_seconds)` — scales with the family's nominal candle length rather than the shrinking TTE.
- Tightened stale-data ceilings: 2s for btc_5m, 3s for btc_15m, 5s for btc_1h (operator-overridable).
- `btc_15m` scorer + 30-minute active window + 200-item discovery limit; family now appears in the `market_family` select.
- `AccountState` carries long/short/net/total BTC exposure; `PortfolioEngine.get_account_state` and `get_exposure_summary` feed the correlation gate.
- SQLite hygiene: WAL + synchronous=NORMAL + temp_store=MEMORY + indexes on every hot lookup column. Events.jsonl auto-prunes via bounded tail-reads and `prune_events_jsonl`; see SQLite & Log-Growth Risk section below.

**Tests landing with the phase:** `tests/test_risk_profiles.py`, `tests/test_btc_15m_family.py`, `tests/test_journal_retention.py` — full suite at 224 green.

### Phase 5 — Operational readiness (daemon, metrics, reconciliation)
- `polymarket-ai-agent daemon` CLI runs forever, auto-reconnect WS.
- `/api/metrics` (Prometheus text) + `/api/healthz`.
- Kill-switch hooks.
- systemd unit + log rotation + SQLite backup cron in `docs/DEPLOYMENT.md`.

### Phase 6 — Front-end + operator UX upgrades (optional)
- SSE-driven dashboard with per-family panels.
- RiskProfile editor in settings.

---

## SQLite & Log-Growth Risk Analysis

The daemon is a long-running append-heavy writer; on a small VPS the two
persistence layers can quietly become a bomb if left alone. This is the
audit we did before Phase 5.

### What the existing writers look like
- [engine/portfolio.py](../src/polymarket_ai_agent/engine/portfolio.py) inserts into three tables every trade cycle: `positions` (on paper fill or live fill bridge), `order_attempts` (every execute call, success or failure), and `live_orders` (each submitted live order, plus status updates on reconciliation).
- [engine/journal.py](../src/polymarket_ai_agent/engine/journal.py) persists `reports` rows via `save_report`, and appends every event to `events.jsonl` via `log_event`. The daemon fires a `daemon_tick` event on every quant decision — at `DAEMON_DECISION_MIN_INTERVAL_SECONDS=1.0` that's ~86k rows/day per active market before Phase 1 rate-limits.

### Concrete blow-up risks (ranked)

1. **events.jsonl grows without bound.** Biggest risk. ~200–400 bytes per daemon_tick × 4 active markets × 1 Hz ≈ 5–15 GB/week. With the old `read_text().splitlines()` tail reader, every `/api/events/stream` or CLI `report` call would OOM once the file crossed RAM. *Addressed in Phase 4*: bounded tail-reads via `Journal._tail_lines` (64KB chunks, backwards) and `prune_events_jsonl`; `log_event` auto-prunes every 200 writes when `events_jsonl_max_bytes` is set (default 200MB with a 50MB tail).
2. **`order_attempts` rows are never pruned.** Every cycle writes one row; counted rejections feed `get_rejected_orders`, which filters by `substr(recorded_at, 1, 10) = today`. That substring predicate was a table scan until Phase 4 added `order_attempts_recorded_at_idx`. Still unbounded in row count — Phase 5 should add a retention helper that deletes attempts older than N days.
3. **`positions` with no index on `status`.** `list_open_positions` and `positions_due_for_close` filter on status on every call, and the daemon calls them many times per minute. *Addressed in Phase 4*: `positions_status_idx` + `positions_market_status_idx` + `positions_closed_at_idx`.
4. **`live_orders` row-churn via status updates.** Each reconciliation tick can rewrite every non-terminal row. *Addressed in Phase 4*: `live_orders_status_idx` + `live_orders_updated_at_idx`; Phase 5 can add terminal-row archival.
5. **Default rollback-journal mode serializes reads and writes.** With a busy daemon writing and the operator API / CLI reading the same DB, locks can stall both. *Addressed in Phase 4*: `PRAGMA journal_mode = WAL` + `synchronous = NORMAL` in both `PortfolioEngine._init_db` and `Journal._init_db`. This also trades a small durability window for much better read concurrency.
6. **Synchronous sqlite calls on an asyncio loop.** The daemon's decision callback ends with `await asyncio.to_thread(journal.log_event, ...)` which is fine today, but direct sync calls from `PortfolioEngine` run in the foreground. Fine for our write volume on a local disk; becomes a problem if the DB ever lives on a network volume. Phase 5 should move portfolio writes behind `asyncio.to_thread` for the same reason.
7. **WAL file on crash.** WAL mode creates `<db>.wal` and `<db>-shm` sidecar files. Unclean shutdown can leave a large WAL; on next open SQLite checkpoints it. Add `PRAGMA wal_autocheckpoint` tuning and a periodic `PRAGMA wal_checkpoint(TRUNCATE)` in the Phase 5 daemon loop.
8. **No backup/rotation.** A single disk failure kills all realised PnL, position, and journal history. Phase 5 should ship a systemd cron that copies `agent.db` + `events.jsonl` to an off-host location nightly. SQLite's `.backup` / `VACUUM INTO` are safe even with live writers once WAL is on.
9. **JSONL payloads contain large nested dicts (auth dumps, book events).** A single pathological event can be tens of KB; truncate `Journal._normalize` to elide or cap very large lists. Lower priority but worth flagging.

### What's now covered vs. still open

Covered in Phase 4: WAL, indexes, bounded `events.jsonl` reads, auto-prune, indexes on reports(created_at).

Still open (explicit Phase 5 work):
- retention / archival job for `order_attempts` and terminal `live_orders` rows (`delete where recorded_at < :cutoff`).
- periodic `VACUUM` or `VACUUM INTO` for long-lived deployments.
- off-host backup (`VACUUM INTO` + rsync / S3 put nightly).
- `asyncio.to_thread` around portfolio writes in the daemon hot path.
- WAL checkpoint loop and size alerting.
- structured metric for DB size and events.jsonl size exposed on `/api/metrics`.

## Out of Scope

- ML fair-value beyond closed-form starting point.
- Multi-asset expansion (ETH, etc.).
- On-chain fill verification beyond `py-clob-client`.
- RL / automated strategy selection.

---

## Verification Strategy (across phases)

1. `make test` stays green.
2. Paper soak per phase: 4h btc_1h, 2h btc_15m, 1h btc_5m. Record hit rate, captured vs projected edge, cancel ratio, latency.
3. Walk-forward replay on journaled events; each model must beat previous on same window.
4. $1 live smoke after Phase 3 and Phase 5.
5. Dashboards show open positions, daily PnL, WS lag, model vs market edge, rejection histogram.

---

## The Three Biggest Wins

1. Wire up Polymarket + BTC websockets + asyncio daemon (Phase 1).
2. Correct per-side edge formula with quant fair value (Phase 2).
3. SELL + cancel/replace + live-fill → position bridge (Phase 3).
