"""Show which trading strategies are active and their key settings.

Reads the current *effective* settings (baseline + SettingsStore overrides)
via the same helper the daemon uses on startup and reload, so the output
matches what the live engine sees.
"""
from __future__ import annotations

import argparse

from polymarket_trading_engine.config import editable_values_snapshot, get_effective_settings


# Per-strategy setting groups. Each entry is (strategy_id, enabled_field_or_None,
# always_on, list_of_setting_keys_to_render). `always_on=True` means the
# strategy fires whenever the daemon is running and there is no toggle.
STRATEGY_GROUPS: list[tuple[str, str | None, bool, list[str]]] = [
    (
        "fade (quant_scoring)",
        None,
        True,
        [
            "fade_post_only",
            "min_edge",
            "min_confidence",
            "max_position_usd",
            "quant_drift_damping",
            "quant_imbalance_tilt",
            "quant_max_abs_edge",
            "quant_invert_drift",
            "quant_trend_filter_enabled",
            "quant_trend_opposed_strong_min_edge",
            "quant_trend_opposed_weak_min_edge",
            "quant_ofi_gate_enabled",
            "quant_vol_regime_enabled",
        ],
    ),
    (
        "adaptive_v1 (regime-router)",
        "adaptive_enabled",
        False,
        [
            "adaptive_enabled",
        ],
    ),
    (
        "adaptive_v2 (overreaction)",
        "adaptive_v2_enabled",
        False,
        [
            "adaptive_v2_enabled",
            "adaptive_v2_post_only",
            "adaptive_v2_overreaction_threshold",
            "adaptive_v2_sensitivity",
            "adaptive_v2_cost_floor",
            "adaptive_v2_min_seconds_to_expiry",
            "adaptive_v2_max_abs_edge",
            "adaptive_v2_invert",
            "adaptive_v2_stop_loss_pct",
        ],
    ),
    (
        "penny (tail-bounce)",
        "penny_enabled",
        False,
        [
            "penny_enabled",
            "penny_entry_thresh",
            "penny_min_entry_tte_seconds",
            "penny_force_exit_tte_seconds",
            "penny_tp_multiple",
            "penny_stop_loss_multiple",
            "penny_size_usd",
            "penny_min_favorable_move_bps",
        ],
    ),
    (
        "market_maker",
        "mm_enabled",
        False,
        [
            "mm_enabled",
            "mm_size_usd",
            "mm_target_half_spread",
            "mm_min_market_spread",
            "mm_max_market_spread",
            "mm_min_tte_seconds",
            "mm_inventory_skew_strength",
            "mm_max_inventory_usd",
            "mm_quote_ttl_seconds",
            "mm_force_exit_tte_seconds",
            "mm_universe_enabled",
            "mm_universe_max_markets",
            "mm_universe_min_rewards_daily_usd",
            "mm_universe_min_liquidity_usd",
            "mm_require_rewards",
        ],
    ),
]

# Settings shared by paper exits — apply to fade and adaptive_v2 only.
SHARED_PAPER_EXIT_KEYS = [
    "paper_stop_loss_pct",
    "paper_trailing_stop_pct",
    "paper_trail_arm_pct",
    "paper_trail_confirmation_ticks",
    "paper_take_profit_pct",
    "paper_tp_ladder",
    "position_force_exit_tte_seconds",
    "paper_sl_limit_ttl_ticks",
]

CONTEXT_KEYS = [
    "trading_mode",
    "market_family",
    "paper_starting_balance_per_strategy",
    "daemon_auto_paper_execute",
]


def fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def render_table(rows: list[tuple[str, object]]) -> str:
    if not rows:
        return "_(no settings to show)_"
    width = max(len(k) for k, _ in rows)
    out = ["| Setting | Value |", "|---|---|"]
    for k, v in rows:
        out.append(f"| `{k}` | `{fmt(v)}` |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-shared-exits", action="store_true", default=True,
                    help="Include shared paper-exit settings under fade/adaptive_v2 (default on)")
    args = ap.parse_args()

    settings = get_effective_settings()
    editable = editable_values_snapshot(settings)
    _MISSING = object()

    def lookup(field: str) -> object:
        v = editable.get(field, _MISSING)
        if v is _MISSING:
            v = getattr(settings, field, _MISSING)
        return "<missing>" if v is _MISSING else v

    snap = {field: lookup(field) for group in STRATEGY_GROUPS for field in group[3]}
    for k in SHARED_PAPER_EXIT_KEYS + CONTEXT_KEYS:
        snap[k] = lookup(k)

    # Top: context + active/inactive summary
    print("# Strategy status\n")

    print("## Context\n")
    print(render_table([(k, snap.get(k, "<missing>")) for k in CONTEXT_KEYS]))
    print()

    print("## Active strategies\n")
    print("| Strategy | Status |")
    print("|---|---|")
    for name, toggle, always_on, _ in STRATEGY_GROUPS:
        active = always_on or bool(snap.get(toggle))
        glyph = "🟢" if active else "🔴"
        print(f"| {name} | {glyph} |")
    print()

    # Per-strategy detail
    for name, toggle, always_on, keys in STRATEGY_GROUPS:
        if always_on:
            active = True
        else:
            active = bool(snap.get(toggle))
        header = f"## {name}"
        if not active:
            header += " — disabled"
        print(f"\n{header}\n")
        rows = [(k, snap.get(k, "<missing>")) for k in keys]
        if name.startswith(("fade", "adaptive_v2")) and active and args.include_shared_exits:
            rows.append(("— shared paper-exit settings —", ""))
            rows.extend((k, snap.get(k, "<missing>")) for k in SHARED_PAPER_EXIT_KEYS)
        print(render_table(rows))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
