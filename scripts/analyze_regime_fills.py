#!/usr/bin/env python3
"""Regime-conditioned fill analysis: why does adaptive have a poor win rate?

Joins every ``position_closed`` event to its nearest-preceding
``daemon_tick`` (same strategy_id + market_id, logged_at <= opened_at),
reads the regime label the scorer was seeing at entry, and aggregates
per-strategy / per-regime PnL stats.

The goal is to answer two questions the aggregate soak report can't:
  1. Does the regime classifier pick winning directions? (TRENDING_UP
     entries → YES wins; TRENDING_DOWN → NO wins; RANGING → neutral.)
  2. Are specific regimes consistently unprofitable so we can gate
     entries in those regimes instead of relying on the SL to bail us
     out after the fact?

Usage:
    python scripts/analyze_regime_fills.py
    python scripts/analyze_regime_fills.py --events logs/events.jsonl
    python scripts/analyze_regime_fills.py --events <backup>/events.jsonl
    python scripts/analyze_regime_fills.py --max-entry-offset-seconds 30
"""
from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class Fill:
    strategy_id: str
    market_id: str
    opened_at: datetime
    side: str
    entry_price: float
    exit_price: float
    realized_pnl: float
    close_reason: str
    hold_seconds: float
    # Regime fields resolved by joining to the nearest daemon_tick.
    regime: str | None = None
    suggested_side: str | None = None
    tick_edge: float | None = None
    tick_offset_seconds: float | None = None  # tick.logged_at - opened_at


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _load_fills(events_path: Path) -> list[Fill]:
    out: list[Fill] = []
    with events_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event_type") != "position_closed":
                continue
            p = rec.get("payload", {})
            opened_at = _parse_ts(p.get("opened_at"))
            if opened_at is None:
                continue
            try:
                out.append(
                    Fill(
                        strategy_id=str(p.get("strategy_id") or "fade"),
                        market_id=str(p.get("market_id", "")),
                        opened_at=opened_at,
                        side=str(p.get("side", "")),
                        entry_price=float(p.get("entry_price") or 0.0),
                        exit_price=float(p.get("exit_price") or 0.0),
                        realized_pnl=float(p.get("realized_pnl") or 0.0),
                        close_reason=str(p.get("close_reason", "")),
                        hold_seconds=float(p.get("hold_seconds") or 0.0),
                    )
                )
            except (TypeError, ValueError):
                continue
    return out


def _build_tick_index(
    events_path: Path,
) -> dict[tuple[str, str], tuple[list[datetime], list[dict]]]:
    """Map (strategy_id, market_id) → (sorted_timestamps, aligned_payloads).

    Stream-parsed so we don't hold the full 100MB+ events file in memory
    twice. Payloads are kept small — just the regime/side/edge fields.
    """
    buf: dict[tuple[str, str], list[tuple[datetime, dict]]] = defaultdict(list)
    with events_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event_type") != "daemon_tick":
                continue
            ts = _parse_ts(rec.get("logged_at"))
            if ts is None:
                continue
            p = rec.get("payload", {})
            key = (str(p.get("strategy_id") or "fade"), str(p.get("market_id", "")))
            if not key[1]:
                continue
            # Trim to just the fields we use — the full payload is huge.
            slim = {
                "regime": p.get("regime"),
                "suggested_side": p.get("suggested_side"),
                "edge_yes": p.get("edge_yes"),
                "edge_no": p.get("edge_no"),
            }
            buf[key].append((ts, slim))
    # Sort once per key; the bisect is per fill.
    out: dict[tuple[str, str], tuple[list[datetime], list[dict]]] = {}
    for key, entries in buf.items():
        entries.sort(key=lambda item: item[0])
        times = [e[0] for e in entries]
        payloads = [e[1] for e in entries]
        out[key] = (times, payloads)
    return out


def _attach_regimes(
    fills: list[Fill],
    tick_index: dict[tuple[str, str], tuple[list[datetime], list[dict]]],
    max_offset_seconds: float,
) -> None:
    """Walk each fill, find the latest daemon_tick logged_at <= opened_at,
    within ``max_offset_seconds``. Leaves fill.regime None when no tick is
    close enough — that's a data gap, not a RANGING regime.
    """
    for fill in fills:
        key = (fill.strategy_id, fill.market_id)
        entry = tick_index.get(key)
        if entry is None:
            continue
        times, payloads = entry
        # bisect_right: rightmost index where times[idx] <= opened_at.
        idx = bisect_right(times, fill.opened_at) - 1
        if idx < 0:
            continue
        offset = (fill.opened_at - times[idx]).total_seconds()
        if offset > max_offset_seconds:
            continue
        p = payloads[idx]
        fill.regime = (p.get("regime") or None)
        fill.suggested_side = p.get("suggested_side")
        # Edge on the side we actually took.
        if fill.side == "YES":
            fill.tick_edge = p.get("edge_yes")
        elif fill.side == "NO":
            fill.tick_edge = p.get("edge_no")
        fill.tick_offset_seconds = offset


def _print_per_regime(fills: list[Fill]) -> None:
    """Per-(strategy, regime) aggregation: counts, win rate, total / avg PnL,
    and close-reason mix so we can tell whether losses are SL blow-outs or
    something else.
    """
    by_key: dict[tuple[str, str], list[Fill]] = defaultdict(list)
    for f in fills:
        if f.regime is None:
            continue
        by_key[(f.strategy_id, f.regime)].append(f)

    unmatched = sum(1 for f in fills if f.regime is None)
    print("\n=== Per-(strategy, regime) breakdown ===")
    print(f"  {'strategy':10} {'regime':15} {'n':>4}  {'win%':>6}  {'total':>8}  {'avg':>7}  {'wrst':>7}")
    for key in sorted(by_key.keys()):
        strategy, regime = key
        fs = by_key[key]
        n = len(fs)
        wins = sum(1 for f in fs if f.realized_pnl > 0)
        total = sum(f.realized_pnl for f in fs)
        worst = min((f.realized_pnl for f in fs), default=0.0)
        win_rate = wins / n * 100.0 if n else 0.0
        print(
            f"  {strategy:10} {regime:15} {n:>4}  {win_rate:>5.1f}%  "
            f"{total:>+8.2f}  {total/n:>+7.3f}  {worst:>+7.3f}"
        )
    if unmatched:
        print(f"  [unmatched] {unmatched} fills had no daemon_tick within the offset window")


def _print_regime_accuracy(fills: list[Fill]) -> None:
    """Does the regime classifier's directional signal match what the scorer
    then picked, and does the combination make money? Breaks out by
    (regime, side_taken) so we can see e.g. "adaptive picks YES in
    TRENDING_DOWN" — a mismatch that hints at a scorer bug.
    """
    print("\n=== Regime → side-taken PnL grid (adaptive only) ===")
    grid: dict[tuple[str, str], list[float]] = defaultdict(list)
    for f in fills:
        if f.strategy_id != "adaptive" or f.regime is None:
            continue
        grid[(f.regime, f.side)].append(f.realized_pnl)
    if not grid:
        print("  (no adaptive fills with regime labels)")
        return
    print(f"  {'regime':15} {'side':4} {'n':>4}  {'win%':>6}  {'total':>8}  {'avg':>7}")
    for key in sorted(grid.keys()):
        regime, side = key
        pnls = grid[key]
        n = len(pnls)
        wins = sum(1 for x in pnls if x > 0)
        total = sum(pnls)
        win_rate = wins / n * 100.0 if n else 0.0
        print(
            f"  {regime:15} {side:4} {n:>4}  {win_rate:>5.1f}%  "
            f"{total:>+8.2f}  {total/n:>+7.3f}"
        )


def _print_close_reason_by_regime(fills: list[Fill]) -> None:
    """Within each regime, how often does the SL fire vs TP vs trail? Tells
    us whether a given regime is being closed by risk controls (bad entries)
    or by profitability (good entries).
    """
    print("\n=== Close-reason mix by regime (adaptive only) ===")
    grid: dict[tuple[str, str], int] = defaultdict(int)
    reasons: set[str] = set()
    regimes: set[str] = set()
    for f in fills:
        if f.strategy_id != "adaptive" or f.regime is None:
            continue
        grid[(f.regime, f.close_reason)] += 1
        reasons.add(f.close_reason)
        regimes.add(f.regime)
    if not grid:
        print("  (no adaptive fills with regime labels)")
        return
    reasons_sorted = sorted(reasons)
    header = f"  {'regime':15}  " + "  ".join(f"{r[:16]:>16}" for r in reasons_sorted)
    print(header)
    for regime in sorted(regimes):
        cells = [f"{grid[(regime, r)]:>16}" for r in reasons_sorted]
        print(f"  {regime:15}  " + "  ".join(cells))


def _print_aggregate(fills: list[Fill]) -> None:
    print("\n=== Aggregate ===")
    total_fills = len(fills)
    matched = sum(1 for f in fills if f.regime is not None)
    print(f"  total fills        : {total_fills}")
    print(f"  fills with regime  : {matched} ({matched / total_fills * 100:.0f}% match rate)")
    if matched > 0:
        offsets = [f.tick_offset_seconds for f in fills if f.tick_offset_seconds is not None]
        offsets.sort()
        mid = offsets[len(offsets) // 2]
        print(f"  median tick-offset : {mid:.1f}s before opened_at")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", default="logs/events.jsonl", help="Path to events.jsonl")
    parser.add_argument(
        "--max-entry-offset-seconds",
        type=float,
        default=60.0,
        help="Max seconds between the preceding daemon_tick and opened_at to accept a join",
    )
    args = parser.parse_args()

    events_path = Path(args.events)
    if not events_path.exists():
        raise SystemExit(f"events file not found: {events_path}")

    print(f"Reading {events_path} ...")
    fills = _load_fills(events_path)
    print(f"Loaded {len(fills)} position_closed events")
    tick_index = _build_tick_index(events_path)
    print(f"Indexed ticks for {len(tick_index)} (strategy, market) pairs")
    _attach_regimes(fills, tick_index, max_offset_seconds=args.max_entry_offset_seconds)

    _print_aggregate(fills)
    _print_per_regime(fills)
    _print_regime_accuracy(fills)
    _print_close_reason_by_regime(fills)


if __name__ == "__main__":
    main()
