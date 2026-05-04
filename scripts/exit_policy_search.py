"""Grid-search exit policies for fade / adaptive_v2 against the live event log.

Loads `logs/events.jsonl`, reconstructs each closed position's price journey
from `daemon_tick` records, fetches market resolutions from Polymarket, then
re-simulates exits under different (stop_loss_pct, trail_arm_pct, trail_pct,
force_exit_tte_seconds) policies and reports the best.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_soak import fetch_outcome  # noqa: E402


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Position:
    market_id: str
    strategy: str
    side: str  # "YES" or "NO"
    size_usd: float
    entry_price: float
    opened_at: datetime
    end_date: datetime
    actual_close_reason: str
    actual_pnl: float


@dataclass
class Tick:
    ts: datetime
    bid_yes: float
    ask_yes: float
    bid_no: float
    ask_no: float
    seconds_to_expiry: int


def load_events(path: Path) -> tuple[list[Position], dict[str, list[Tick]]]:
    positions: list[Position] = []
    ticks: dict[str, list[Tick]] = defaultdict(list)
    with path.open() as f:
        for line in f:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            et = ev.get("event_type")
            p = ev.get("payload", {})
            if et == "position_closed":
                strategy = p.get("strategy_id") or ""
                if strategy not in {"fade", "adaptive_v2"}:
                    continue
                positions.append(Position(
                    market_id=str(p.get("market_id")),
                    strategy=strategy,
                    side=str(p.get("side")),
                    size_usd=float(p.get("size_usd") or 0.0),
                    entry_price=float(p.get("entry_price") or 0.0),
                    opened_at=parse_iso(str(p.get("opened_at"))),
                    end_date=parse_iso(str(p.get("end_date_iso"))),
                    actual_close_reason=str(p.get("close_reason")),
                    actual_pnl=float(p.get("realized_pnl") or 0.0),
                ))
            elif et == "daemon_tick":
                mid = str(p.get("market_id"))
                if mid is None:
                    continue
                try:
                    ts = parse_iso(str(ev["logged_at"]))
                except Exception:
                    continue
                ticks[mid].append(Tick(
                    ts=ts,
                    bid_yes=float(p.get("bid_yes") or 0.0),
                    ask_yes=float(p.get("ask_yes") or 0.0),
                    bid_no=float(p.get("bid_no") or 0.0),
                    ask_no=float(p.get("ask_no") or 0.0),
                    seconds_to_expiry=int(p.get("seconds_to_expiry") or 0),
                ))
    for mid in ticks:
        ticks[mid].sort(key=lambda t: t.ts)
    return positions, ticks


def fetch_outcomes(positions: list[Position]) -> dict[str, str]:
    market_ids = sorted({p.market_id for p in positions})
    outcomes: dict[str, str] = {}
    with httpx.Client() as client:
        for mid in market_ids:
            outcome, _ = fetch_outcome(mid, client)
            if outcome:
                outcomes[mid] = outcome
    return outcomes


def shares(size_usd: float, entry_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return size_usd / entry_price


def pnl_at_price(pos: Position, exit_price: float) -> float:
    """PnL if we sold our position at `exit_price` (price = the side we hold)."""
    s = shares(pos.size_usd, pos.entry_price)
    return s * (exit_price - pos.entry_price)


def pnl_at_resolution(pos: Position, outcome: str) -> float:
    """PnL if held to expiry; binary payoff."""
    s = shares(pos.size_usd, pos.entry_price)
    won = (outcome == pos.side)
    final = 1.0 if won else 0.0
    return s * (final - pos.entry_price)


@dataclass
class Policy:
    sl_pct: float          # stop loss; e.g., 0.30 = -30% on cost
    trail_arm_pct: float   # arm trail when up by this much; e.g., 0.20
    trail_pct: float       # trail width once armed; e.g., 0.15
    force_exit_tte: int    # close when seconds_to_expiry <= this


def simulate(pos: Position, journey: list[Tick], outcome: str | None, policy: Policy) -> float:
    """Re-simulate exits. Returns realized PnL."""
    if pos.entry_price <= 0:
        return 0.0
    side = pos.side
    relevant = [t for t in journey if pos.opened_at < t.ts <= pos.end_date]
    peak = pos.entry_price  # price of our side at entry; we use bid for valuation
    armed = False
    for t in relevant:
        # Mark-to-bid for the side we hold (what we could sell at)
        cur_bid = t.bid_yes if side == "YES" else t.bid_no
        cur_ask = t.ask_yes if side == "YES" else t.ask_no
        # Use bid for trailing/stop (what we actually realize on exit)
        if cur_bid <= 0:
            continue
        # Stop-loss check: if bid drops far below cost, exit
        if policy.sl_pct > 0 and cur_bid <= pos.entry_price * (1.0 - policy.sl_pct):
            return pnl_at_price(pos, cur_bid)
        # Trail arming
        if not armed:
            up_pct = (cur_bid - pos.entry_price) / pos.entry_price
            if up_pct >= policy.trail_arm_pct:
                armed = True
                peak = cur_bid
        else:
            if cur_bid > peak:
                peak = cur_bid
            # Trailing stop: trigger if dropped trail_pct from peak (relative to peak)
            if peak > 0 and (peak - cur_bid) / peak >= policy.trail_pct:
                return pnl_at_price(pos, cur_bid)
        # Force exit at TTE
        if t.seconds_to_expiry <= policy.force_exit_tte:
            return pnl_at_price(pos, cur_bid)
    # Fell through: held to expiry
    if outcome is None:
        return 0.0
    return pnl_at_resolution(pos, outcome)


def evaluate(positions: list[Position], journeys: dict[str, list[Tick]], outcomes: dict[str, str], policy: Policy) -> tuple[float, int, int]:
    total = 0.0
    wins = 0
    for pos in positions:
        journey = journeys.get(pos.market_id, [])
        outcome = outcomes.get(pos.market_id)
        pnl = simulate(pos, journey, outcome, policy)
        total += pnl
        if pnl > 0:
            wins += 1
    return total, wins, len(positions)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--strategy", choices=["fade", "adaptive_v2", "both"], default="both")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    print("Loading events...", flush=True)
    positions, journeys = load_events(Path(args.events))
    print(f"  {len(positions)} closed positions, {sum(len(v) for v in journeys.values())} ticks across {len(journeys)} markets")

    if args.strategy != "both":
        positions = [p for p in positions if p.strategy == args.strategy]
        print(f"  filtered to strategy={args.strategy}: {len(positions)} positions")

    print("Fetching market resolutions...", flush=True)
    outcomes = fetch_outcomes(positions)
    resolved = sum(1 for p in positions if p.market_id in outcomes)
    print(f"  resolved: {resolved}/{len(positions)}")

    # Baseline: actual PnL recorded in the journal
    actual_total = sum(p.actual_pnl for p in positions)
    actual_wins = sum(1 for p in positions if p.actual_pnl > 0)
    print(f"\nActual realized: pnl={actual_total:+.2f}  wins={actual_wins}/{len(positions)} ({actual_wins/len(positions)*100:.1f}%)")

    # Grid
    sl_grid = [0.30, 0.40, 0.50, 0.60, 0.80, 0.95]
    arm_grid = [0.0, 0.10, 0.20, 0.30]
    trail_grid = [0.10, 0.15, 0.25, 0.40, 0.95]
    force_grid = [10, 30, 45, 60]

    results = []
    for sl, arm, tr, fe in product(sl_grid, arm_grid, trail_grid, force_grid):
        pol = Policy(sl_pct=sl, trail_arm_pct=arm, trail_pct=tr, force_exit_tte=fe)
        total, wins, n = evaluate(positions, journeys, outcomes, pol)
        results.append((total, wins, n, pol))

    results.sort(key=lambda r: r[0], reverse=True)
    print(f"\n=== Top {args.top} policies ({len(results)} tested) ===")
    print(f"{'pnl':>8} {'wr':>6} {'sl':>6} {'arm':>6} {'trail':>6} {'fe':>4}")
    for total, wins, n, pol in results[:args.top]:
        wr = wins / n * 100 if n else 0
        print(f"  {total:+7.2f} {wr:5.1f}% {pol.sl_pct:5.2f} {pol.trail_arm_pct:5.2f} {pol.trail_pct:5.2f} {pol.force_exit_tte:4d}")

    print(f"\n=== Bottom 5 policies ===")
    for total, wins, n, pol in results[-5:]:
        wr = wins / n * 100 if n else 0
        print(f"  {total:+7.2f} {wr:5.1f}% sl={pol.sl_pct:.2f} arm={pol.trail_arm_pct:.2f} trail={pol.trail_pct:.2f} fe={pol.force_exit_tte}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
