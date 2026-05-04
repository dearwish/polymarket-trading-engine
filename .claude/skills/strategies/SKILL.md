---
name: strategies
description: Show which trading strategies are currently active in the polymarket-trading-engine soak and the key settings driving each one. Reads the live effective settings (baseline + SettingsStore overrides from data/agent.db) so the output reflects what the running daemon sees, including any hot-reload changes since startup.
allowed-tools: Bash(uv run python scripts/show_strategies.py*)
argument-hint: (no args)
---

# Show active strategies

Render a per-strategy view of what's enabled, what's disabled, and the key knobs governing each one. Pulls effective settings from `polymarket_trading_engine.config.get_effective_settings()` — the same path the daemon uses on reload — so the output stays accurate whether the daemon is running or not.

## Steps

### 1. Run the helper

```sh
uv run python scripts/show_strategies.py
```

The script prints a complete markdown report:
- `## Context` — runtime context (mode, market family, per-strategy bankrolls).
- `## Active strategies` — one-line summary table of every strategy and its enabled status.
- `## <strategy>` — one detail section per strategy with the relevant settings as a `Setting | Value` table. For `fade` and `adaptive_v2`, the shared paper-exit settings are appended under a divider row so they're visible in the same place as the entry knobs that drive them.

### 2. Surface the output

Pass the report through verbatim. Do NOT summarize, re-format, or omit sections — the user wants the full table view, including the disabled strategies (so they can confirm at a glance that nothing snuck back on).

### 3. Failure modes

- If the helper exits non-zero, print stderr verbatim and stop. Don't try to reconstruct the report by querying SQLite directly.
- If `uv` is missing, fall back to `python scripts/show_strategies.py` from the project venv.
- If the script prints `<missing>` for a setting, that means the field exists on the Settings model but isn't in the editable snapshot — leave it as `<missing>` rather than guessing.

## Notes

- This is read-only — it never writes to `settings_changes`. To change a setting, use a separate write path (API, dashboard, or a tuning script).
- The "always on" tag for `fade` is structural, not a setting: the QuantScoringEngine has no enable toggle. `fade_post_only` controls whether its entries route through the paper-maker lifecycle, not whether it scores.
- Per-strategy bankrolls live in `paper_starting_balance_per_strategy` (CSV `strategy_id:amount,...`) and are shown once in the Context table, not duplicated under each strategy.
