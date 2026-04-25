from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "polymarket-ai-agent"
    trading_mode: str = "paper"
    market_family: str = "btc_1h"
    loop_seconds: int = 15

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-4.1-mini"

    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_data_url: str = "https://data-api.polymarket.com"
    polymarket_ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    polymarket_chain_id: int = 137
    btc_ws_url: str = "wss://stream.binance.com:9443/stream"
    btc_symbol: str = "btcusdt"
    btc_rest_fallback_url: str = "https://api.binance.com/api/v3/ticker/price"
    ws_reconnect_backoff_seconds: float = 2.0
    ws_reconnect_backoff_max_seconds: float = 30.0
    ws_ssl_verify: bool = True
    daemon_discovery_interval_seconds: int = 60
    daemon_decision_min_interval_seconds: float = 1.0
    # How often the daemon polls settings_changes.id for operator overrides.
    # 2s matches human-perceived "instant" and mirrors the other interval
    # loops; see DaemonRunner._settings_reload_loop.
    daemon_settings_reload_interval_seconds: float = 2.0
    # When True the daemon auto-executes APPROVED decisions as paper trades.
    # Positions are recorded in the portfolio so the dashboard / report paths
    # fill in without any separate CLI invocation. Safe by default (opt-in).
    daemon_auto_paper_execute: bool = False
    # Exit thresholds for open paper positions, evaluated on every tick.
    # Expressed as a fraction of the committed size_usd:
    #   paper_take_profit_pct = 0.20 → close when unrealised PnL ≥ +20%
    #   paper_stop_loss_pct   = 0.25 → close when unrealised PnL ≤ −25%
    # Set to 0.0 to disable (default): positions then hold until the TTE exit
    # buffer kicks in right before market resolution.
    paper_take_profit_pct: float = 0.0
    paper_stop_loss_pct: float = 0.0
    # Trailing stop: track peak token price since entry; close when current
    # drops by `paper_trailing_stop_pct` of the peak. 0.0 disables.
    paper_trailing_stop_pct: float = 0.0
    # Trail-arm threshold: the trailing stop only becomes active once the
    # peak clears entry_price × (1 + paper_trail_arm_pct). Prevents the
    # trail from firing at a small loss when the peak barely moved above
    # entry. 0.0 arms immediately (previous behaviour).
    paper_trail_arm_pct: float = 0.0
    # Entry cooldown: after any close on a market, block new entries on the
    # same market for this many seconds. Prevents immediate re-entry whipsaw
    # where the scorer flips sides and each flip gets stopped out. 0 disables.
    paper_entry_cooldown_seconds: int = 0
    # Minimum TTE for a new entry. Trades opened inside the last minute of a
    # candle have no time for the thesis to develop before the exit buffer
    # forces a close, and price noise in the final seconds dominates the GBM
    # drift signal. 0 disables. Typical value: 60–90.
    min_entry_tte_seconds: int = 0
    # Force-close any open position at this TTE regardless of PnL (supersedes
    # the exit_buffer sweep when > exit_buffer_seconds). Lets us widen the
    # fixed stop without holding through the final-seconds noise that eats
    # trailing stops. 0 disables — the normal exit buffer still applies.
    position_force_exit_tte_seconds: int = 0
    # Consecutive-loss circuit breaker. If the most recent N CLOSED positions
    # all have realized_pnl ≤ 0 the daemon halts with safety_stop_reason=
    # "consecutive_loss_limit" until an operator reviews. 0 disables.
    max_consecutive_losses: int = 0
    # Minimum seconds elapsed since the candle opened before a new entry is
    # allowed. Prevents entering at candle open when btc_log_return_since_candle_open
    # is ~0 and GBM uncertainty is at its maximum. Only applied to candle-style
    # families (btc_15m, btc_5m, btc_1h); threshold markets are unaffected.
    # 0 disables (default). A value of 60–90 is a reasonable starting point.
    min_candle_elapsed_seconds: int = 0
    # Upper bound: don't enter after this many seconds into the candle. Avoids
    # entering in the final third where there is no time for the TP/trail to
    # develop but the full stop-loss can still fire. 0 disables.
    max_candle_elapsed_seconds: int = 0
    # Scale-out / tiered take-profit ladder. Comma-separated list of
    #   "<pnl_pct>:<fraction_to_close>" pairs, e.g. "0.15:0.5,0.30:0.25"
    # meaning: at +15% PnL close 50% of the position, at +30% close another
    # 25%. Tranches fire in order; each only once per open position. Empty
    # string disables the ladder entirely (fixed TP / trailing / TTE still
    # apply to the remainder).
    paper_tp_ladder: str = ""
    polymarket_private_key: str = ""
    polymarket_funder: str = ""
    polymarket_signature_type: int = 0
    live_trading_enabled: bool = False
    live_order_type: str = "FOK"
    live_post_only: bool = False

    max_position_usd: float = 10.0
    min_confidence: float = 0.75
    min_edge: float = 0.03
    max_spread: float = 0.04
    min_depth_usd: float = 200.0
    exit_buffer_seconds: int = 5
    exit_buffer_pct_of_tte: float = 0.0
    max_daily_loss_usd: float = 25.0
    stale_data_seconds: int = 30
    max_rejected_orders: int = 3
    max_concurrent_positions: int = 1
    max_net_btc_exposure_usd: float = 50.0
    paper_starting_balance_usd: float = 100.0
    paper_position_ttl_seconds: int = 60
    paper_entry_slippage_bps: float = 10.0
    paper_exit_slippage_bps: float = 10.0
    # Phase 3 adaptive-regime: maker-follow configuration for the adaptive
    # scorer. When it sees a trending regime it places a resting limit a
    # ``paper_follow_limit_discount_bps`` below mid on its side; if the book
    # crosses that level within ``paper_follow_maker_ttl_seconds`` we treat
    # it as filled. 50 bps / 300 s are initial defaults tuned for 15-minute
    # BTC Up/Down markets — revisit after the first trending-regime soak.
    paper_follow_limit_discount_bps: float = 50.0
    paper_follow_maker_ttl_seconds: int = 300
    # Maker-yield selection (Tier 2 from gamma-trade-lab reference, 2026-04):
    # gate cancel/replace on material drift so the follow-maker path can
    # track a moving mid without churning on every tick. Both thresholds
    # default to 0.0 = never re-price a resting order (preserves the
    # existing "rest and wait for TTL" behaviour); raise the price
    # threshold to e.g. 0.005 (half-cent) and the size threshold to 10%
    # to opt in to price tracking.
    paper_follow_cancel_price_threshold: float = 0.0
    paper_follow_cancel_size_threshold_pct: float = 0.0
    # Depth filter for anchoring the maker mid. With min>0 we skip ghost
    # 1-lot levels when computing best-bid/best-ask — prevents the paper
    # maker from posting behind a phantom order. 0.0 disables (raw mid).
    paper_follow_min_level_size_shares: float = 0.0
    # Penny-buy strategy (extreme-tail dip). Runs as a third strategy
    # alongside fade + adaptive. See ``engine/penny_scoring.py`` for the
    # thesis and ``scripts/backtest_penny.py`` for the parameter sweep
    # that chose these defaults. All four settings hot-reload.
    # Phase 1 scorer clone — delegates to fade in every regime (trending
    # path was retired 2026-04-23 after follow-maker hit 17% win rate on
    # 250 trades). With it enabled the daemon doubles market-impact for
    # zero alpha; default off. Flip true only to bootstrap a genuinely
    # different adaptive variant in the strategy slot.
    adaptive_enabled: bool = False
    penny_enabled: bool = True
    penny_entry_thresh: float = 0.03
    penny_min_entry_tte_seconds: int = 300
    penny_force_exit_tte_seconds: int = 120
    penny_tp_multiple: float = 2.0
    penny_size_usd: float = 1.0
    # Stop-loss as a multiple of entry price. 0.5 means "exit when bid
    # drops to 50% of entry" (a 3¢ entry stops out at 1.5¢). Caps each
    # losing trade at −(1 − multiple) × size instead of riding to the
    # TTE-based force-exit floor (typically around 1¢ = −67%). Setting
    # to 0 disables the gate. See scripts/backtest_penny.py for the
    # break-even hit-rate math.
    penny_stop_loss_multiple: float = 0.5
    # Reversal-confirmation gate: require YES mid to have moved IN OUR
    # FAVOUR by at least this many bps over the last 30s before entering.
    # Replaces the earlier "max adverse move" gate which only required a
    # pause — the 2026-04-24 soak showed pauses are often temporary and
    # the side keeps crashing. Requiring actual reversal gives a much
    # stronger "the knife has bounced" signal. 0 disables.
    penny_min_favorable_move_bps: float = 25.0
    # Overreaction-fade strategy (adaptive_v2). Runs as a third scorer
    # alongside fade + penny; orthogonal signal: detects Polymarket mid
    # moves that outpace BTC's justification and bets the reversion. See
    # engine/overreaction_scoring.py for the full thesis.
    adaptive_v2_enabled: bool = True
    adaptive_v2_overreaction_threshold: float = 0.02
    adaptive_v2_sensitivity: float = 10.0
    adaptive_v2_cost_floor: float = 0.005
    adaptive_v2_min_seconds_to_expiry: int = 60

    fee_bps: float = 0.0
    execution_maker_min_edge: float = 0.04
    execution_maker_min_tte_seconds: int = 120
    execution_price_tick: float = 0.01
    # Hysteresis on maker cancel/replace: only re-quote when the fresh maker
    # price has moved by more than ``execution_replace_min_ticks`` × tick OR
    # when the resting size differs from a target size by more than
    # ``execution_replace_min_size_pct``. Default of 2 ticks (warproxxx /
    # gamma-trade-lab pattern) prevents the cancel-thrash that kills queue
    # position when our scorer re-fires on every WS event with sub-tick noise.
    execution_replace_min_ticks: float = 2.0
    execution_replace_min_size_pct: float = 0.10
    execution_exit_buffer_floor_seconds: int = 10
    execution_exit_buffer_pct_of_tte: float = 0.1
    quant_drift_damping: float = 0.5
    # When True the scorer flips fair_yes to (1 - fair_yes). Used to test the
    # hypothesis that BTC short-horizon moves mean-revert rather than follow
    # GBM continuation: if the unflipped hit rate is significantly < 50% with
    # a strong Brier score, flipping should land the signal in the 60-90% band.
    quant_invert_drift: bool = False
    # Hard abstain ceiling on the chosen-side edge. Empirical soak data showed
    # the highest-conviction picks (|edge| ≥ 0.30) had the worst hit rates,
    # likely because extreme edges come from model/market disagreements the
    # GBM prior can't actually resolve. Set > 0 to force ABSTAIN whenever the
    # chosen edge exceeds the ceiling; 0.0 disables the guard.
    quant_max_abs_edge: float = 0.0
    quant_imbalance_tilt: float = 0.03
    quant_slippage_baseline_bps: float = 15.0
    quant_slippage_spread_coef: float = 0.25
    quant_default_vol_per_second: float = 0.00015
    quant_drift_horizon_seconds: float = 900.0
    quant_tte_floor_seconds: float = 5.0
    quant_confidence_per_edge: float = 10.0
    quant_high_expiry_risk_seconds: int = 15
    quant_medium_expiry_risk_seconds: int = 60
    # Shadow scorer (Phase 4 A/B). Empty string disables; set to "htf_tilt" to
    # run a parallel scorer on every tick without affecting live trade logic.
    # When enabled, daemon_tick gains shadow_fair_probability / shadow_suggested_side
    # / shadow_edge_yes / shadow_edge_no fields for offline comparison.
    # Regime-aware gate (replaces the old binary trend veto).
    # When enabled, counter-trend trades must clear a higher minimum edge
    # proportional to trend strength. With-trend and ranging trades are
    # unaffected. 4h takes priority over 1h (highest-timeframe-wins).
    quant_trend_filter_enabled: bool = False
    quant_trend_filter_min_abs_return: float = 0.003
    # Required edge when the chosen side opposes the 4h / 1h trend.
    quant_trend_opposed_strong_min_edge: float = 0.15  # vs 4h trend
    quant_trend_opposed_weak_min_edge: float = 0.06    # vs 1h trend only
    # Distressed market floor: block counter-trend entry when the ask on our side
    # is below this price. A low ask means the market has already heavily priced
    # in the opposing outcome — the GBM edge is structural lag, not real alpha.
    # E.g. 0.30 blocks buying YES when ask_yes < 0.30 in a downtrend. 0 = off.
    quant_trend_distressed_max_ask: float = 0.0
    # Unconditional minimum entry price: skip any trade where our side's ask is
    # below this floor regardless of trend direction. Very low ask prices (< 0.20)
    # have bid-ask spreads that exceed the stop-loss width, causing immediate
    # gap-out on entry. 0 = off.
    quant_min_entry_price: float = 0.0

    # OFI gate: veto trades where strong signed order flow opposes the direction.
    # Informed flow (signed_flow_5s) is the single best short-term price-impact
    # predictor per Cont et al. 2014. Only fires when |flow| >= min_abs_flow.
    quant_ofi_gate_enabled: bool = False
    quant_ofi_gate_min_abs_flow: float = 30.0

    # Volatility regime gate: raise edge bar or abstain in high-vol conditions
    # where GBM fair-value estimates are less reliable (wider confidence intervals).
    quant_vol_regime_enabled: bool = False
    quant_vol_regime_high_threshold: float = 0.005    # 30m realized vol
    quant_vol_regime_extreme_threshold: float = 0.010
    quant_vol_regime_high_min_edge: float = 0.08

    quant_shadow_variant: str = ""
    # Magnitude of the HTF-trend tilt applied by the shadow scorer.
    # sign(btc_log_return_1h) * this value is added to the base fair_yes.
    # Calibrated from soak: 1h return predicted 57% of outcomes (vs 50% random)
    # while the base scorer agreed with the trend only 41% of the time.
    quant_shadow_htf_tilt_strength: float = 0.10
    # Per-session additive bias for the shadow scorer. Derived from observed
    # fair_yes vs actual YES-rate gaps in the first HTF soak (EU −14pp, US −27pp).
    # These are regime-conditional and should be re-calibrated after each soak.
    quant_shadow_session_bias_eu: float = 0.0
    quant_shadow_session_bias_us: float = 0.0

    events_jsonl_max_bytes: int = 200_000_000
    events_jsonl_keep_tail_bytes: int = 50_000_000
    daemon_maintenance_interval_seconds: int = 3600
    daemon_heartbeat_interval_seconds: float = 5.0
    daemon_prune_history_days: int = 14
    daemon_heartbeat_stale_seconds: float = 30.0
    daemon_ws_stale_seconds: float = 60.0

    data_dir: Path = Field(default=Path("data"))
    log_dir: Path = Field(default=Path("logs"))
    db_path: Path = Field(default=Path("data/agent.db"))
    events_path: Path = Field(default=Path("logs/events.jsonl"))
    heartbeat_path: Path = Field(default=Path("data/daemon_heartbeat.json"))
    backups_dir: Path = Field(default=Path("data/backups"))
    runtime_settings_path: Path = Field(default=Path("data/runtime_settings.json"))


EDITABLE_SETTINGS_METADATA: dict[str, dict[str, Any]] = {
    # Every field here must also appear in INITIAL_SETTINGS_BASELINE — enforced
    # by tests/test_initial_settings.py. Fields marked requires_restart=True
    # are still editable via the API/CLI but the daemon's reload loop surfaces
    # the flag to the operator instead of hot-swapping them.
    "trading_mode": {"label": "Mode", "type": "select", "options": ["paper", "live"], "group": "runtime", "requires_restart": True},
    "market_family": {
        "label": "Market Family",
        "type": "select",
        "options": ["btc_1h", "btc_15m", "btc_5m", "btc_daily_threshold"],
        "group": "runtime",
        "requires_restart": True,
    },
    "loop_seconds": {"label": "Loop Seconds", "type": "number", "min": 1, "max": 300, "step": 1, "group": "runtime"},
    "openrouter_model": {"label": "OpenRouter Model", "type": "text", "group": "runtime"},
    "daemon_auto_paper_execute": {
        "label": "Daemon Auto Paper Execute",
        "type": "boolean",
        "group": "runtime",
        "requires_restart": True,
    },
    "live_trading_enabled": {"label": "Live Trading Enabled", "type": "boolean", "group": "live"},
    "live_order_type": {"label": "Live Order Type", "type": "select", "options": ["FOK", "GTC"], "group": "live"},
    "live_post_only": {"label": "Live Post Only", "type": "boolean", "group": "live"},
    "max_position_usd": {"label": "Max Position USD", "type": "number", "min": 1, "max": 100000, "step": 0.5, "group": "thresholds"},
    "min_confidence": {"label": "Min Confidence", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "min_edge": {"label": "Min Edge", "type": "number", "min": 0, "max": 1, "step": 0.001, "group": "thresholds"},
    "max_spread": {"label": "Max Spread", "type": "number", "min": 0, "max": 1, "step": 0.001, "group": "thresholds"},
    "min_depth_usd": {"label": "Min Depth USD", "type": "number", "min": 0, "max": 1000000, "step": 1, "group": "thresholds"},
    "exit_buffer_seconds": {"label": "Exit Buffer Seconds", "type": "number", "min": 0, "max": 3600, "step": 1, "group": "thresholds"},
    "max_daily_loss_usd": {"label": "Max Daily Loss USD", "type": "number", "min": 0, "max": 100000, "step": 0.5, "group": "thresholds"},
    "stale_data_seconds": {"label": "Stale Data Seconds", "type": "number", "min": 1, "max": 3600, "step": 1, "group": "thresholds"},
    "max_rejected_orders": {"label": "Max Rejected Orders", "type": "number", "min": 1, "max": 100, "step": 1, "group": "thresholds"},
    "max_concurrent_positions": {"label": "Max Concurrent Positions", "type": "number", "min": 1, "max": 20, "step": 1, "group": "thresholds"},
    "max_net_btc_exposure_usd": {"label": "Max Net BTC Exposure USD", "type": "number", "min": 0, "max": 1000000, "step": 1, "group": "thresholds"},
    "exit_buffer_pct_of_tte": {"label": "Exit Buffer % of TTE", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "paper_starting_balance_usd": {
        "label": "Paper Starting Balance USD",
        "type": "number",
        "min": 0,
        "max": 1000000,
        "step": 1,
        "group": "paper",
        "requires_restart": True,
    },
    "paper_position_ttl_seconds": {
        "label": "Paper Position TTL Seconds",
        "type": "number",
        "min": 1,
        "max": 86400,
        "step": 1,
        "group": "paper",
    },
    "paper_entry_slippage_bps": {
        "label": "Paper Entry Slippage BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 0.1,
        "group": "paper",
    },
    "paper_exit_slippage_bps": {
        "label": "Paper Exit Slippage BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 0.1,
        "group": "paper",
    },
    "paper_follow_limit_discount_bps": {
        "label": "Follow Maker Discount BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 1,
        "group": "paper",
    },
    "paper_follow_maker_ttl_seconds": {
        "label": "Follow Maker TTL (seconds)",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "paper",
    },
    "paper_follow_cancel_price_threshold": {
        "label": "Follow Maker Cancel Price Threshold",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.001,
        "group": "paper",
    },
    "paper_follow_cancel_size_threshold_pct": {
        "label": "Follow Maker Cancel Size Threshold (%)",
        "type": "number",
        "min": 0,
        "max": 100,
        "step": 0.1,
        "group": "paper",
    },
    "paper_follow_min_level_size_shares": {
        "label": "Follow Maker Min Level Size (shares)",
        "type": "number",
        "min": 0,
        "max": 100000,
        "step": 1,
        "group": "paper",
    },
    "adaptive_enabled": {
        "label": "Adaptive (V1, fade-clone) Enabled",
        "type": "boolean",
        "group": "paper",
    },
    "penny_enabled": {
        "label": "Penny Strategy Enabled",
        "type": "boolean",
        "group": "paper",
    },
    "penny_entry_thresh": {
        "label": "Penny Entry Threshold (ask ≤)",
        "type": "number",
        "min": 0,
        "max": 0.2,
        "step": 0.005,
        "group": "paper",
    },
    "penny_min_entry_tte_seconds": {
        "label": "Penny Min Entry TTE (seconds)",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 10,
        "group": "paper",
    },
    "penny_force_exit_tte_seconds": {
        "label": "Penny Force-Exit TTE (seconds)",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 10,
        "group": "paper",
    },
    "penny_tp_multiple": {
        "label": "Penny TP Multiple (× entry)",
        "type": "number",
        "min": 1,
        "max": 100,
        "step": 0.25,
        "group": "paper",
    },
    "penny_size_usd": {
        "label": "Penny Position Size (USD)",
        "type": "number",
        "min": 0.1,
        "max": 100,
        "step": 0.1,
        "group": "paper",
    },
    "penny_stop_loss_multiple": {
        "label": "Penny Stop-Loss Multiple (× entry)",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.05,
        "group": "paper",
    },
    "penny_min_favorable_move_bps": {
        "label": "Penny Min Favorable Move (bps over 30s)",
        "type": "number",
        "min": 0,
        "max": 2000,
        "step": 5,
        "group": "paper",
    },
    "adaptive_v2_enabled": {
        "label": "Adaptive V2 (Overreaction-Fade) Enabled",
        "type": "boolean",
        "group": "paper",
    },
    "adaptive_v2_overreaction_threshold": {
        "label": "Adaptive V2 Overreaction Threshold",
        "type": "number",
        "min": 0,
        "max": 0.5,
        "step": 0.005,
        "group": "paper",
    },
    "adaptive_v2_sensitivity": {
        "label": "Adaptive V2 BTC→PM Sensitivity",
        "type": "number",
        "min": 0.5,
        "max": 50,
        "step": 0.5,
        "group": "paper",
    },
    "adaptive_v2_cost_floor": {
        "label": "Adaptive V2 Cost Floor (edge subtracted)",
        "type": "number",
        "min": 0,
        "max": 0.1,
        "step": 0.001,
        "group": "paper",
    },
    "adaptive_v2_min_seconds_to_expiry": {
        "label": "Adaptive V2 Min TTE (seconds)",
        "type": "number",
        "min": 0,
        "max": 900,
        "step": 10,
        "group": "paper",
    },
    "paper_take_profit_pct": {
        "label": "Paper Take Profit %",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "paper_stop_loss_pct": {
        "label": "Paper Stop Loss %",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "paper_trailing_stop_pct": {
        "label": "Paper Trailing Stop %",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "paper_tp_ladder": {
        "label": "Paper TP Ladder (pct:fraction,...)",
        "type": "text",
        "group": "paper",
    },
    "paper_trail_arm_pct": {
        "label": "Paper Trail Arm %",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "paper_entry_cooldown_seconds": {
        "label": "Paper Entry Cooldown Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "paper",
    },
    "min_entry_tte_seconds": {
        "label": "Min Entry TTE Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "thresholds",
    },
    "position_force_exit_tte_seconds": {
        "label": "Position Force-Exit TTE Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "paper",
    },
    "max_consecutive_losses": {
        "label": "Max Consecutive Losses",
        "type": "number",
        "min": 0,
        "max": 100,
        "step": 1,
        "group": "thresholds",
    },
    "min_candle_elapsed_seconds": {
        "label": "Min Candle Elapsed Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "thresholds",
    },
    "max_candle_elapsed_seconds": {
        "label": "Max Candle Elapsed Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "thresholds",
    },
    "fee_bps": {
        "label": "Fee BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 0.1,
        "group": "paper",
    },
    # Quant scorer gates
    "quant_invert_drift": {"label": "Invert Drift", "type": "boolean", "group": "thresholds"},
    "quant_drift_damping": {"label": "Drift Damping", "type": "number", "min": 0, "max": 5, "step": 0.05, "group": "thresholds"},
    "quant_max_abs_edge": {"label": "Max |Edge| Ceiling", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_trend_filter_enabled": {"label": "Trend Filter Enabled", "type": "boolean", "group": "thresholds"},
    "quant_trend_filter_min_abs_return": {"label": "Trend Filter Min |Return|", "type": "number", "min": 0, "max": 0.1, "step": 0.0005, "group": "thresholds"},
    "quant_trend_opposed_strong_min_edge": {"label": "Counter-Trend Min Edge (4h)", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_trend_opposed_weak_min_edge": {"label": "Counter-Trend Min Edge (1h)", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_trend_distressed_max_ask": {"label": "Distressed Max Ask", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_min_entry_price": {"label": "Min Entry Price", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_ofi_gate_enabled": {"label": "OFI Gate Enabled", "type": "boolean", "group": "thresholds"},
    "quant_ofi_gate_min_abs_flow": {"label": "OFI Gate Min |Flow|", "type": "number", "min": 0, "max": 10000, "step": 1, "group": "thresholds"},
    "quant_vol_regime_enabled": {"label": "Vol Regime Enabled", "type": "boolean", "group": "thresholds"},
    "quant_vol_regime_high_threshold": {"label": "Vol Regime High Threshold", "type": "number", "min": 0, "max": 1, "step": 0.0005, "group": "thresholds"},
    "quant_vol_regime_extreme_threshold": {"label": "Vol Regime Extreme Threshold", "type": "number", "min": 0, "max": 1, "step": 0.0005, "group": "thresholds"},
    "quant_vol_regime_high_min_edge": {"label": "Vol Regime High Min Edge", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_shadow_variant": {"label": "Shadow Variant", "type": "text", "group": "thresholds"},
    "quant_shadow_htf_tilt_strength": {"label": "Shadow HTF Tilt Strength", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_shadow_session_bias_eu": {"label": "Shadow Session Bias (EU)", "type": "number", "min": -1, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_shadow_session_bias_us": {"label": "Shadow Session Bias (US)", "type": "number", "min": -1, "max": 1, "step": 0.01, "group": "thresholds"},
}

# Mark fields requires_restart=True on their metadata entries above. Mirrors
# initial_settings.REQUIRES_RESTART so callers that touch config.py don't
# have to import two modules.
REQUIRES_RESTART_FIELDS: frozenset[str] = frozenset(
    key for key, meta in EDITABLE_SETTINGS_METADATA.items() if meta.get("requires_restart")
)



# Cache Settings, but invalidate when .env changes so a live edit (e.g. MARKET_FAMILY)
# takes effect without restarting the API / daemon process.
_ENV_FILE = Path(".env")
_settings_cache: dict[str, Any] = {"settings": None, "env_mtime": None}


def _env_mtime() -> float | None:
    try:
        return _ENV_FILE.stat().st_mtime if _ENV_FILE.exists() else None
    except OSError:
        return None


def get_settings() -> Settings:
    current_mtime = _env_mtime()
    cached = _settings_cache["settings"]
    if cached is not None and _settings_cache["env_mtime"] == current_mtime:
        return cached
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.events_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
    _settings_cache["settings"] = settings
    _settings_cache["env_mtime"] = current_mtime
    return settings


def _settings_store_for(settings: Settings):
    # Local import avoids a config ↔ engine cycle at module import time.
    from polymarket_ai_agent.engine.settings_store import SettingsStore

    return SettingsStore(settings.db_path)


def load_runtime_overrides(settings: Settings) -> dict[str, Any]:
    """Return the editable-field overrides currently in the DB.

    Filtered through ``EDITABLE_SETTINGS_METADATA`` so stray rows for fields
    that have since been removed from the whitelist don't leak into the
    effective settings. Safe to call before the DB has been initialised;
    returns ``{}`` if the table doesn't exist yet.
    """
    try:
        overrides = _settings_store_for(settings).current_overrides()
    except Exception:
        # DB not yet migrated (table missing) or unreadable — treat as no
        # overrides and let the caller fall back to code defaults.
        return {}
    return {key: value for key, value in overrides.items() if key in EDITABLE_SETTINGS_METADATA}


def save_runtime_overrides(settings: Settings, updates: dict[str, Any]) -> dict[str, Any]:
    """Persist ``updates`` as new ``settings_changes`` rows and return the
    materialised effective overrides after the write.

    Only fields that (a) appear in ``EDITABLE_SETTINGS_METADATA`` and (b)
    differ from the current effective value are recorded — idempotent writes
    don't pollute the audit trail.
    """
    editable_updates = {key: value for key, value in updates.items() if key in EDITABLE_SETTINGS_METADATA}
    if not editable_updates:
        return load_runtime_overrides(settings)
    # Validate via pydantic so string→int/bool coercion matches the Settings
    # class — keeps the DB clean of weird string "true" values where bools
    # are expected.
    current_overrides = load_runtime_overrides(settings)
    candidate = Settings.model_validate({**settings.model_dump(), **current_overrides, **editable_updates})
    effective_now = Settings.model_validate({**settings.model_dump(), **current_overrides})
    changes: list[tuple[str, Any, Any]] = []
    for key in editable_updates:
        new_value = getattr(candidate, key)
        old_value = getattr(effective_now, key)
        if new_value != old_value:
            changes.append((key, old_value, new_value))
    if changes:
        _settings_store_for(settings).record_changes(changes, source="api")
    return load_runtime_overrides(settings)


def get_effective_settings() -> Settings:
    base = get_settings()
    overrides = load_runtime_overrides(base)
    if not overrides:
        return base
    effective = Settings.model_validate({**base.model_dump(), **overrides})
    effective.data_dir.mkdir(parents=True, exist_ok=True)
    effective.log_dir.mkdir(parents=True, exist_ok=True)
    effective.db_path.parent.mkdir(parents=True, exist_ok=True)
    effective.events_path.parent.mkdir(parents=True, exist_ok=True)
    effective.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
    return effective


def diff_editable(old: Settings, new: Settings) -> dict[str, dict[str, Any]]:
    """Return ``{field: {'before': old_value, 'after': new_value}}`` for every
    editable field whose value changed between ``old`` and ``new``.

    Used by the daemon reload loop to emit precise ``settings_changed``
    journal events and by the API handler to skip no-op writes.
    """
    out: dict[str, dict[str, Any]] = {}
    for field in EDITABLE_SETTINGS_METADATA:
        before = getattr(old, field, None)
        after = getattr(new, field, None)
        if before != after:
            out[field] = {"before": before, "after": after}
    return out


def editable_values_snapshot(settings: Settings) -> dict[str, Any]:
    """Flat ``{field: value}`` snapshot of every editable field — the shape the
    daemon emits on startup as the baseline for audit segmentation.
    """
    return {field: getattr(settings, field, None) for field in EDITABLE_SETTINGS_METADATA}


def runtime_settings_payload(settings: Settings) -> dict[str, Any]:
    overrides = load_runtime_overrides(settings)
    return {
        "values": {key: getattr(settings, key) for key in EDITABLE_SETTINGS_METADATA},
        "overrides": overrides,
        "fields": EDITABLE_SETTINGS_METADATA,
    }


@dataclass(slots=True, frozen=True)
class RiskProfile:
    """Resolved per-family risk parameters consumed by :class:`RiskEngine`.

    The profile is derived from :class:`Settings` via :func:`resolve_risk_profile`:
    if a field was explicitly overridden on ``Settings`` the override wins, so
    existing deployments that tune globals via env vars are unchanged. If the
    field is left at the built-in default and the active ``market_family`` has
    a per-family override in :data:`FAMILY_PROFILE_OVERRIDES`, the tighter
    family value is applied.
    """

    family: str
    min_edge: float
    max_spread: float
    min_depth_usd: float
    stale_data_seconds: int
    exit_buffer_floor_seconds: int
    exit_buffer_pct_of_tte: float
    family_window_seconds: int
    max_position_usd: float
    max_concurrent_positions: int
    max_net_btc_exposure_usd: float


FAMILY_PROFILE_OVERRIDES: dict[str, dict[str, Any]] = {
    "btc_1h": {
        "stale_data_seconds": 5,
        "exit_buffer_pct_of_tte": 0.05,
        "max_concurrent_positions": 2,
        "family_window_seconds": 3600,
    },
    "btc_15m": {
        "stale_data_seconds": 3,
        "exit_buffer_pct_of_tte": 0.07,
        "max_concurrent_positions": 2,
        "family_window_seconds": 900,
    },
    "btc_5m": {
        "stale_data_seconds": 2,
        "exit_buffer_pct_of_tte": 0.10,
        "max_concurrent_positions": 1,
        "family_window_seconds": 300,
    },
}


_PROFILE_FIELD_TO_SETTING = {
    "stale_data_seconds": "stale_data_seconds",
    "exit_buffer_floor_seconds": "exit_buffer_seconds",
    "exit_buffer_pct_of_tte": "exit_buffer_pct_of_tte",
    "max_position_usd": "max_position_usd",
    "max_concurrent_positions": "max_concurrent_positions",
}


def resolve_risk_profile(settings: Settings) -> RiskProfile:
    family = settings.market_family
    overrides = FAMILY_PROFILE_OVERRIDES.get(family, {})
    explicit = settings.model_fields_set

    def pick(profile_field: str) -> Any:
        settings_field = _PROFILE_FIELD_TO_SETTING.get(profile_field, profile_field)
        # Explicit operator override wins.
        if settings_field in explicit:
            return getattr(settings, settings_field)
        if profile_field in overrides:
            return overrides[profile_field]
        return getattr(settings, settings_field)

    family_window = int(overrides.get("family_window_seconds", 0))
    return RiskProfile(
        family=family,
        min_edge=settings.min_edge,
        max_spread=settings.max_spread,
        min_depth_usd=settings.min_depth_usd,
        stale_data_seconds=int(pick("stale_data_seconds")),
        exit_buffer_floor_seconds=int(pick("exit_buffer_floor_seconds")),
        exit_buffer_pct_of_tte=float(pick("exit_buffer_pct_of_tte")),
        family_window_seconds=family_window,
        max_position_usd=float(pick("max_position_usd")),
        max_concurrent_positions=int(pick("max_concurrent_positions")),
        max_net_btc_exposure_usd=float(settings.max_net_btc_exposure_usd),
    )
