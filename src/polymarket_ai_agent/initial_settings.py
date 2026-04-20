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
    "min_edge": 0.10,
    "max_spread": 0.04,
    "min_depth_usd": 200.0,
    "exit_buffer_seconds": 5,
    "exit_buffer_pct_of_tte": 0.0,
    "max_daily_loss_usd": 20.0,
    "max_consecutive_losses": 0,
    "max_rejected_orders": 3,
    "max_net_btc_exposure_usd": 50.0,
    "stale_data_seconds": 30,
    "min_entry_tte_seconds": 90,
    # --- Paper exit ladder ---
    "paper_take_profit_pct": 0.0,
    "paper_stop_loss_pct": 0.20,
    "paper_trailing_stop_pct": 0.15,
    "paper_trail_arm_pct": 0.12,
    "paper_tp_ladder": "0.30:0.5",
    "paper_entry_cooldown_seconds": 120,
    "position_force_exit_tte_seconds": 45,
    "min_candle_elapsed_seconds": 60,
    "max_candle_elapsed_seconds": 660,
    "paper_starting_balance_usd": 100.0,
    "paper_position_ttl_seconds": 60,
    "paper_entry_slippage_bps": 10.0,
    "paper_exit_slippage_bps": 10.0,
    "fee_bps": 0.0,
    # --- Quant scorer gates ---
    "quant_invert_drift": False,
    "quant_max_abs_edge": 0.25,
    "quant_trend_filter_enabled": True,
    "quant_trend_filter_min_abs_return": 0.003,
    "quant_trend_opposed_strong_min_edge": 0.25,
    "quant_trend_opposed_weak_min_edge": 0.06,
    "quant_trend_distressed_max_ask": 0.30,
    "quant_min_entry_price": 0.30,
    "quant_ofi_gate_enabled": True,
    "quant_ofi_gate_min_abs_flow": 60.0,
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
