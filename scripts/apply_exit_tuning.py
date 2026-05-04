"""Apply exit-policy tuning derived from scripts/exit_policy_search.py.

Records changes to the SettingsStore so the daemon picks them up on next
reload (or next start). Idempotent: re-running with the same target values
inserts no rows.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from polymarket_trading_engine.engine.settings_store import SettingsStore


TARGETS: dict[str, object] = {
    # Fixed stop-loss: effectively disabled. Binary payoff bounds max loss to entry cost.
    "paper_stop_loss_pct": 0.95,
    # Trailing stop: arm later (let position prove itself), trail wider (don't trim winners).
    "paper_trail_arm_pct": 0.15,
    "paper_trailing_stop_pct": 0.40,
    # Adaptive v2 also disables fixed SL via its per-strategy override.
    "adaptive_v2_stop_loss_pct": 0.95,
    # Entry filter: edge in [0.15, 0.20) bucket lost on both strategies (adverse selection
    # during breakouts). Edge >= 0.20 was the only profitable bucket. Raise the floor.
    "min_edge": 0.20,
    # Note: position_force_exit_tte_seconds left at 45 (current optimum).
    # paper_tp_ladder kept ("0.20:0.25,0.40:0.25,0.60:0.25") — captures gains on the way up.
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/agent.db")
    ap.add_argument("--reason", default="exit-policy grid-search 2026-05-04: SL too tight, trail too narrow")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = SettingsStore(Path(args.db))
    overrides = store.current_overrides()

    pending = []
    for field, target in TARGETS.items():
        before = overrides.get(field)
        if before == target:
            print(f"  skip  {field}: already {target!r}")
            continue
        print(f"  set   {field}: {before!r} -> {target!r}")
        pending.append((field, before, target))

    if args.dry_run:
        print(f"\n[dry-run] {len(pending)} changes pending; not written.")
        return 0
    if not pending:
        print("\nNothing to apply.")
        return 0

    ids = store.record_changes(
        pending,
        source="cli",
        actor="exit_tuning_script",
        reason=args.reason,
    )
    print(f"\nWrote {len(ids)} settings_changes rows: ids={ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
