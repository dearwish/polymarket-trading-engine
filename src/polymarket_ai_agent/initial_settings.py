"""Curated starting values for every operator-tunable setting.

Seeded into ``settings_changes`` on first boot by the
``20260421T140000-seed-initial-settings-baseline.py`` migration. Evolutions
happen through ``settings_changes`` rows (dashboard / CLI / API) — this
constant is the starting point, never mutated at runtime.

To start a clean A/B soak: back up ``data/agent.db``, delete it, and restart —
the daemon re-seeds from this constant. Tweak a value here to change the
starting point of the next soak without touching runtime state.

Fields here must also appear in ``EDITABLE_SETTINGS_METADATA``; the key-set
invariant is enforced by ``tests/test_initial_settings.py``.
"""
from __future__ import annotations

from typing import Any

INITIAL_SETTINGS_BASELINE: dict[str, Any] = {
    # --- Runtime / structural (requires_restart on trading_mode + market_family) ---
    "trading_mode": "paper",
    "market_family": "btc_15m",
    "loop_seconds": 15,
    "openrouter_model": "openai/gpt-4.1-mini",
    "daemon_auto_paper_execute": True,
    # --- Live-trading gates ---
    "live_trading_enabled": False,
    "live_order_type": "FOK",
    "live_post_only": False,
    # --- Risk thresholds ---
    "max_position_usd": 2.0,
    "max_concurrent_positions": 1,
    "min_confidence": 0.60,
    "min_edge": 0.15,
    "max_spread": 0.04,
    "min_depth_usd": 200.0,
    "exit_buffer_seconds": 5,
    "exit_buffer_pct_of_tte": 0.0,
    "max_daily_loss_usd": 100.0,
    "max_consecutive_losses": 0,
    "max_rejected_orders": 3,
    "max_net_btc_exposure_usd": 50.0,
    "stale_data_seconds": 30,
    "min_entry_tte_seconds": 90,
    # --- Paper exit ladder ---
    "paper_take_profit_pct": 0.0,
    "paper_stop_loss_pct": 0.15,
    "paper_trailing_stop_pct": 0.15,
    "paper_trail_arm_pct": 0.12,
    "paper_trail_confirmation_ticks": 3,
    "paper_sl_limit_ttl_ticks": 5,
    "paper_sl_limit_slippage_ticks": 2,
    "min_exit_depth_multiplier": 2.0,
    "paper_tp_ladder": "0.20:0.25,0.40:0.25,0.60:0.25",
    "paper_entry_cooldown_seconds": 120,
    "position_force_exit_tte_seconds": 45,
    "min_candle_elapsed_seconds": 60,
    "max_candle_elapsed_seconds": 660,
    "paper_starting_balance_usd": 100.0,
    # Empty by default → every strategy uses ``paper_starting_balance_usd``.
    # Operator can flip this to e.g. ``"market_maker:10000,fade:200"`` to
    # give each paper strategy an independent bankroll for honest
    # side-by-side soak comparison.
    "paper_starting_balance_per_strategy": "",
    "paper_position_ttl_seconds": 60,
    "paper_entry_slippage_bps": 10.0,
    "paper_exit_slippage_bps": 10.0,
    "paper_follow_limit_discount_bps": 50.0,
    "paper_follow_maker_ttl_seconds": 300,
    # Tier 2a/2b enabled for the 2026-04-23 maker-yield soak. Price threshold
    # is half a cent; size threshold is 10% (matches the gamma-trade-lab
    # reference gates). Depth filter skips sub-10-share ghost levels when
    # anchoring the maker mid. All three can be tuned live via the dashboard
    # without restart.
    "paper_follow_cancel_price_threshold": 0.005,
    "paper_follow_cancel_size_threshold_pct": 10.0,
    "paper_follow_min_level_size_shares": 10.0,
    # Off by default. Flip true to soak the fade scorer through the
    # paper-maker lifecycle (resting limit, TTL, hysteresis) — the
    # paper-mode equivalent of live_post_only=GTC.
    "fade_post_only": False,
    # Penny-buy strategy defaults (from 8h backtest sweep, 2026-04-23):
    # entry_thresh=0.03 + TTE≥300s + TP=2x produced 63.6% hit, +46% ROI
    # on n=11 trades. Paper mode, parallel to fade + adaptive.
    # Adaptive V1 is a fade-clone since 2026-04-23 (trending branch retired
    # after empirical failure). Off by default — toggle via dashboard only
    # to seed a new adaptive variant experiment.
    "adaptive_enabled": False,
    "penny_enabled": True,
    "penny_entry_thresh": 0.03,
    "penny_min_entry_tte_seconds": 300,
    "penny_force_exit_tte_seconds": 120,
    "penny_tp_multiple": 2.0,
    "penny_size_usd": 1.0,
    # Stop-loss as a fraction of entry. 0.5 caps each loser at −50%
    # instead of the TTE-floor of roughly −67% (observed live 2026-04-24).
    "penny_stop_loss_multiple": 0.5,
    # Require YES mid to have moved in our favour by ≥ 25 bps in the
    # last 30s — stricter than the earlier "no adverse move" gate,
    # which permitted pauses that then resumed downward. 2026-04-24
    # live data: all post-gate ticks abstained on violent adverse
    # moves (5000+ bps); this reversal-confirmation gate is the next
    # refinement to filter the slow-bleed cases too.
    "penny_min_favorable_move_bps": 25.0,
    # Adaptive V2 — overreaction-fade. Default threshold 2% (mid moved
    # 2%+ faster than BTC justifies); sensitivity 10 = a 1% BTC move is
    # "expected" to move mid 10 percentage points, calibrated roughly
    # from the GBM derivative at a 0.5-mid 5-min market.
    "adaptive_v2_enabled": True,
    "adaptive_v2_overreaction_threshold": 0.02,
    "adaptive_v2_sensitivity": 10.0,
    "adaptive_v2_cost_floor": 0.005,
    "adaptive_v2_min_seconds_to_expiry": 60,
    "adaptive_v2_max_abs_edge": 0.30,
    "adaptive_v2_post_only": True,
    "adaptive_v2_stop_loss_pct": 0.10,
    "adaptive_v2_invert": True,
    # Market-maker strategy (V1). Off by default; flip ``mm_enabled`` true
    # to soak it side-by-side with the directional scorers. Defaults are
    # the conservative starting point: $1 per leg, 2¢ half-spread, 10¢
    # toxic-spread cutoff, $5 inventory cap per side.
    "mm_enabled": False,
    "mm_size_usd": 5.0,
    "mm_target_half_spread": 0.02,
    "mm_min_market_spread": 0.01,
    "mm_max_market_spread": 0.10,
    "mm_min_tte_seconds": 120,
    "mm_inventory_skew_strength": 0.5,
    "mm_max_inventory_usd": 5.0,
    "mm_require_rewards": False,
    "mm_quote_ttl_seconds": 60,
    "mm_replace_min_ticks": 1.0,
    "mm_replace_min_size_pct": 0.10,
    "mm_force_exit_tte_seconds": 30,
    "mm_freshness_interval_seconds": 5.0,
    # Adverse-selection guards (added 2026-05-03 after the soak surfaced
    # two −$985 single-leg fills caused by stale-quote-on-resolution).
    "mm_max_fill_drift_pct": 5.0,
    "mm_no_fill_tte_seconds": 60,
    "mm_max_quote_age_seconds": 600,
    # MM universe scanner (Phase 2 — yield-based market selection).
    # Enabled by default whenever ``mm_enabled`` is true; the scanner
    # itself is a no-op if mm_enabled is false.
    "mm_universe_enabled": True,
    "mm_universe_min_rewards_daily_usd": 1.0,
    "mm_universe_min_liquidity_usd": 5000.0,
    "mm_universe_min_tte_seconds": 3600,
    "mm_universe_max_markets": 5,
    "mm_universe_refresh_seconds": 300,
    # ON by default: don't quote markets where our $5 size is below the
    # reward-pool min. The filter is a no-op until ``mm_enabled`` flips
    # on, so this default doesn't affect non-MM soaks.
    "mm_universe_require_size_eligible": True,
    "fee_bps": 0.0,
    # --- Quant scorer gates ---
    "quant_invert_drift": True,
    "quant_drift_damping": 0.5,
    "quant_max_abs_edge": 0.25,
    "quant_trend_filter_enabled": True,
    "quant_trend_filter_min_abs_return": 0.003,
    "quant_trend_opposed_strong_min_edge": 0.25,
    "quant_trend_opposed_weak_min_edge": 0.06,
    "quant_trend_distressed_max_ask": 0.30,
    "quant_min_entry_price": 0.32,
    "quant_max_entry_price": 0.50,
    "quant_ofi_gate_enabled": True,
    "quant_ofi_gate_min_abs_flow": 25.0,
    "quant_vol_regime_enabled": True,
    "quant_vol_regime_high_threshold": 0.005,
    "quant_vol_regime_extreme_threshold": 0.008,
    "quant_vol_regime_high_min_edge": 0.08,
    "quant_shadow_variant": "htf_tilt",
    "quant_shadow_htf_tilt_strength": 0.05,
    "quant_shadow_session_bias_eu": 0.0,
    "quant_shadow_session_bias_us": 0.0,
}


# Fields that should block a hot-swap; the reload loop surfaces the flag but
# leaves the engines alone so operators know an explicit restart is required.
REQUIRES_RESTART: frozenset[str] = frozenset({
    "trading_mode",
    "market_family",
    "daemon_auto_paper_execute",
    "paper_starting_balance_usd",
})
