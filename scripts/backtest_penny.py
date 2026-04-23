#!/usr/bin/env python3
"""Backtest the penny-buy strategy against recorded daemon_tick events.

Strategy: when either side's ask drops to ≤ ``entry_thresh`` (default 0.01),
buy ``size_usd`` of that side. Hold until ONE of:

  1. Our side's BID crosses ``entry_price × tp_multiple`` → exit at that bid
     (TP hit — this is the profit case).
  2. The tick stream for that market runs out (market resolved / dropped off
     the feed) → exit at last observed bid on our side. If the side resolved
     worthless (market closed against us), last bid is near 0 and we eat the
     full loss. If it resolved in our favor (rare), we collect near 1.0.

The simulator is intentionally conservative — it requires the bid to CROSS
our TP target in real observed data. No "if we'd been a bit patient" extrapolation.

Usage:
    python scripts/backtest_penny.py --events logs/events.jsonl
    python scripts/backtest_penny.py --events <backup>/events.jsonl --entry-thresh 0.02
    python scripts/backtest_penny.py --tp-multiples 2,3,5,10

The event log must come from a daemon that was subscribed to the relevant
markets throughout their lifetime — backtest accuracy is only as good as
the tick coverage at entry and exit.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(slots=True)
class Tick:
    logged_at: str
    market_id: str
    bid_yes: float
    ask_yes: float
    bid_no: float
    ask_no: float
    seconds_to_expiry: int


@dataclass(slots=True)
class TradeResult:
    market_id: str
    tp_multiple: float
    side: str                  # "YES" | "NO"
    entry_price: float
    exit_price: float
    shares: float
    pnl_usd: float
    pnl_pct: float
    hold_ticks: int
    outcome: str               # "tp_hit" | "expired"


def _load_ticks_by_market(events_path: Path) -> dict[str, list[Tick]]:
    """Stream-parse daemon_tick events, grouped by market_id in arrival order.

    Only keeps the fields the backtest uses, so a 100MB+ events file fits
    comfortably in memory. Ticks from multiple strategy_ids are merged on
    a single market — they observe the same book, so the extra rows are
    just denser sampling.
    """
    grouped: dict[str, list[Tick]] = defaultdict(list)
    with events_path.open() as fh:
        for line in fh:
            if '"daemon_tick"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec.get("payload", {})
            market_id = str(p.get("market_id", ""))
            if not market_id:
                continue
            grouped[market_id].append(
                Tick(
                    logged_at=str(rec.get("logged_at", "")),
                    market_id=market_id,
                    bid_yes=float(p.get("bid_yes") or 0.0),
                    ask_yes=float(p.get("ask_yes") or 0.0),
                    bid_no=float(p.get("bid_no") or 0.0),
                    ask_no=float(p.get("ask_no") or 0.0),
                    seconds_to_expiry=int(p.get("seconds_to_expiry") or 0),
                )
            )
    return grouped


def _simulate_one_tp(
    ticks: list[Tick],
    entry_thresh: float,
    tp_multiple: float,
    size_usd: float,
    min_entry_tte_seconds: int,
    force_exit_tte_seconds: int,
) -> TradeResult | None:
    """One trade per market: first penny-ask entry that passes TTE gates,
    held until TP, until ``force_exit_tte_seconds`` triggers a bail, or
    until the feed ends.

    TTE gates address the core failure mode observed in the unconstrained
    run: setups that appeared with <30s left never got a pullback window,
    so we rode them to worthless. With ``min_entry_tte_seconds`` we skip
    those terminal setups; with ``force_exit_tte_seconds`` we sell at
    whatever bid the book shows before the last-minute liquidity crater.

    Returns None when the market never exposed a sub-threshold ask WITH
    enough TTE to enter — those markets shouldn't count against win rate.
    """
    entry_side: str | None = None
    entry_price: float | None = None
    entry_idx: int | None = None
    tp_target: float | None = None

    for i, tick in enumerate(ticks):
        if entry_side is None:
            # Entry: take whichever side's ask first clears the threshold
            # AND has sufficient TTE remaining. Short-TTE setups are a
            # trap — the bid rarely recovers before resolution.
            if tick.seconds_to_expiry < min_entry_tte_seconds:
                continue
            if 0.0 < tick.ask_no <= entry_thresh:
                entry_side = "NO"
                entry_price = tick.ask_no
                entry_idx = i
                tp_target = entry_price * tp_multiple
                continue
            if 0.0 < tick.ask_yes <= entry_thresh:
                entry_side = "YES"
                entry_price = tick.ask_yes
                entry_idx = i
                tp_target = entry_price * tp_multiple
                continue
        else:
            bid = tick.bid_no if entry_side == "NO" else tick.bid_yes
            # Force-exit window: bail before TTE runs out so we take
            # WHATEVER the bid is showing rather than riding to zero. A
            # partial-loss exit at e.g. 1.5¢ beats a full-loss resolution.
            if (
                force_exit_tte_seconds > 0
                and tick.seconds_to_expiry <= force_exit_tte_seconds
            ):
                shares = size_usd / entry_price  # type: ignore[operator]
                pnl_usd = (bid - entry_price) * shares  # type: ignore[operator]
                pnl_pct = (bid - entry_price) / entry_price  # type: ignore[operator]
                return TradeResult(
                    market_id=tick.market_id,
                    tp_multiple=tp_multiple,
                    side=entry_side,
                    entry_price=entry_price,  # type: ignore[arg-type]
                    exit_price=bid,
                    shares=shares,
                    pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct,
                    hold_ticks=i - entry_idx,  # type: ignore[operator]
                    outcome="force_exit",
                )
            if bid >= tp_target:  # type: ignore[operator]
                shares = size_usd / entry_price  # type: ignore[operator]
                pnl_usd = (bid - entry_price) * shares  # type: ignore[operator]
                pnl_pct = (bid - entry_price) / entry_price  # type: ignore[operator]
                return TradeResult(
                    market_id=tick.market_id,
                    tp_multiple=tp_multiple,
                    side=entry_side,
                    entry_price=entry_price,  # type: ignore[arg-type]
                    exit_price=bid,
                    shares=shares,
                    pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct,
                    hold_ticks=i - entry_idx,  # type: ignore[operator]
                    outcome="tp_hit",
                )

    if entry_side is None:
        return None

    # Feed ended before TP or force-exit fired — rare once force_exit_tte
    # is set, but possible if the daemon dropped the market early.
    last = ticks[-1]
    final_bid = last.bid_no if entry_side == "NO" else last.bid_yes
    shares = size_usd / entry_price  # type: ignore[operator]
    pnl_usd = (final_bid - entry_price) * shares  # type: ignore[operator]
    pnl_pct = (final_bid - entry_price) / entry_price  # type: ignore[operator]
    return TradeResult(
        market_id=last.market_id,
        tp_multiple=tp_multiple,
        side=entry_side,
        entry_price=entry_price,  # type: ignore[arg-type]
        exit_price=final_bid,
        shares=shares,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        hold_ticks=len(ticks) - 1 - entry_idx,  # type: ignore[operator]
        outcome="expired",
    )


def _print_summary(
    results: list[TradeResult],
    tp_multiple: float,
    entry_thresh: float,
    size_usd: float,
) -> None:
    """Single-TP-level summary. Breaks out TP-hit / force-exit / expired
    outcomes because the force-exit path is the new variable — we need
    to know whether it rescued value or just locked in losses earlier.
    """
    n = len(results)
    if n == 0:
        print(f"  TP={tp_multiple}x → no setups detected at entry ≤ {entry_thresh}")
        return
    wins = [r for r in results if r.pnl_usd > 0]
    losses = [r for r in results if r.pnl_usd <= 0]
    tp_hits = [r for r in results if r.outcome == "tp_hit"]
    force_exits = [r for r in results if r.outcome == "force_exit"]
    expired = [r for r in results if r.outcome == "expired"]
    total_pnl = sum(r.pnl_usd for r in results)
    roi_pct = total_pnl / (n * size_usd) * 100.0
    win_rate = len(wins) / n * 100.0
    tp_rate = len(tp_hits) / n * 100.0
    avg_win = sum(r.pnl_usd for r in wins) / len(wins) if wins else 0.0
    avg_loss = sum(r.pnl_usd for r in losses) / len(losses) if losses else 0.0
    fx_avg = sum(r.pnl_usd for r in force_exits) / len(force_exits) if force_exits else 0.0
    print(
        f"  TP={tp_multiple:>4.1f}x  n={n:>3}  "
        f"tp={tp_rate:>4.1f}%  win={win_rate:>4.1f}%  "
        f"total={total_pnl:>+7.3f}  roi={roi_pct:>+6.1f}%  "
        f"avg_win={avg_win:>+5.3f}  avg_loss={avg_loss:>+5.3f}  "
        f"[tp={len(tp_hits)} fx={len(force_exits)}@{fx_avg:+.3f} ex={len(expired)}]"
    )


def _print_side_breakdown(results: list[TradeResult]) -> None:
    """Does the strategy work equally on YES-penny and NO-penny setups?
    An imbalance points to a BTC-trend bias in the event log (e.g. soak
    ran during a strong down move so most penny sides were NO).
    """
    by_side: dict[str, list[TradeResult]] = defaultdict(list)
    for r in results:
        by_side[r.side].append(r)
    for side in sorted(by_side):
        rs = by_side[side]
        total = sum(r.pnl_usd for r in rs)
        wins = sum(1 for r in rs if r.pnl_usd > 0)
        print(
            f"    {side:>3}  n={len(rs):>3}  win={wins/len(rs)*100:>4.1f}%  "
            f"total={total:>+7.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", default="logs/events.jsonl", help="Path to events.jsonl")
    parser.add_argument(
        "--entry-thresh",
        type=float,
        default=0.01,
        help="Buy the side whose ask drops to or below this level (default 0.01 = 1¢)",
    )
    parser.add_argument(
        "--tp-multiples",
        default="2,3,5",
        help="Comma-separated TP levels as multiples of entry price (default 2,3,5)",
    )
    parser.add_argument(
        "--size-usd",
        type=float,
        default=1.0,
        help="Notional size per entry in USD (default 1.0)",
    )
    parser.add_argument(
        "--min-entry-tte-seconds",
        type=int,
        default=0,
        help=(
            "Minimum seconds-to-expiry required at entry (0 = any). "
            "Filters out terminal-cliff setups where there's no time for "
            "a pullback before resolution."
        ),
    )
    parser.add_argument(
        "--force-exit-tte-seconds",
        type=int,
        default=0,
        help=(
            "Force-exit at current bid when TTE drops to this (0 = never). "
            "Salvages partial value before the last-minute liquidity "
            "crater on losing penny bets."
        ),
    )
    args = parser.parse_args()

    events_path = Path(args.events)
    if not events_path.exists():
        raise SystemExit(f"events file not found: {events_path}")
    tp_multiples = [float(x) for x in args.tp_multiples.split(",") if x.strip()]

    print(f"Reading {events_path} ...")
    by_market = _load_ticks_by_market(events_path)
    print(f"Loaded ticks for {len(by_market)} markets")
    print(
        f"Params: entry_thresh={args.entry_thresh}  size_usd=${args.size_usd}  "
        f"tp_multiples={tp_multiples}  "
        f"min_entry_tte={args.min_entry_tte_seconds}s  "
        f"force_exit_tte={args.force_exit_tte_seconds}s"
    )

    print("\n=== Per-TP summary (one trade per market, first penny entry) ===")
    for tp in tp_multiples:
        results: list[TradeResult] = []
        for market_id, ticks in by_market.items():
            result = _simulate_one_tp(
                ticks,
                args.entry_thresh,
                tp,
                args.size_usd,
                args.min_entry_tte_seconds,
                args.force_exit_tte_seconds,
            )
            if result is not None:
                results.append(result)
        _print_summary(results, tp, args.entry_thresh, args.size_usd)
        if results and tp == tp_multiples[0]:
            # Only print side breakdown once (against the tightest TP since
            # it has the most fills) — all TP levels share the same setups.
            print("  by side (at tightest TP):")
            _print_side_breakdown(results)

    # A bit of sanity about coverage: how many markets produced any penny
    # setup at all? If it's <20% the backtest is dominated by idiosyncratic
    # picks and should not be trusted for a go/no-go decision.
    setup_rate = sum(
        1 for ticks in by_market.values()
        if any(
            (0 < t.ask_no <= args.entry_thresh) or (0 < t.ask_yes <= args.entry_thresh)
            for t in ticks
        )
    ) / max(len(by_market), 1)
    print(f"\nSetup rate: {setup_rate * 100:.1f}% of markets exposed a sub-{args.entry_thresh:.02f} ask at least once.")


if __name__ == "__main__":
    main()
