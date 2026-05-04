"""Bucket closed positions by entry-time signal strength to find selectivity gates.

For each closed position, finds the nearest preceding daemon_tick for that
market+strategy whose suggested_side matches the position side, then buckets
by edge / confidence at entry against the held-to-expiry outcome.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_soak import fetch_outcome  # noqa: E402


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--strategy", default="adaptive_v2")
    args = ap.parse_args()

    closes = []
    ticks_by_market = defaultdict(list)
    with open(args.events) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            et = ev.get("event_type")
            p = ev.get("payload", {})
            if et == "position_closed":
                if (p.get("strategy_id") or "") != args.strategy:
                    continue
                closes.append({
                    "market_id": str(p.get("market_id")),
                    "side": str(p.get("side")),
                    "size_usd": float(p.get("size_usd") or 0.0),
                    "entry_price": float(p.get("entry_price") or 0.0),
                    "opened_at": parse_iso(str(p.get("opened_at"))),
                })
            elif et == "daemon_tick":
                if (p.get("strategy_id") or "") != args.strategy:
                    continue
                try:
                    ts = parse_iso(str(ev["logged_at"]))
                except Exception:
                    continue
                ticks_by_market[str(p.get("market_id"))].append({
                    "ts": ts,
                    "suggested_side": p.get("suggested_side"),
                    "edge_yes": float(p.get("edge_yes") or 0.0),
                    "edge_no": float(p.get("edge_no") or 0.0),
                    "confidence": float(p.get("confidence") or 0.0),
                    "fair_probability": float(p.get("fair_probability") or 0.0),
                })

    print(f"Loaded {len(closes)} {args.strategy} closes; {sum(len(v) for v in ticks_by_market.values())} ticks")

    # For each close, find nearest preceding tick where suggested_side == close.side
    enriched = []
    for c in closes:
        candidates = [t for t in ticks_by_market.get(c["market_id"], [])
                      if t["ts"] <= c["opened_at"] and t["suggested_side"] == c["side"]]
        if not candidates:
            continue
        entry_tick = max(candidates, key=lambda t: t["ts"])
        edge = entry_tick["edge_yes"] if c["side"] == "YES" else entry_tick["edge_no"]
        c["entry_edge"] = edge
        c["entry_confidence"] = entry_tick["confidence"]
        c["entry_fair"] = entry_tick["fair_probability"]
        enriched.append(c)

    print(f"  matched entry tick for {len(enriched)} positions")

    # Fetch outcomes
    print("Fetching resolutions...")
    outcomes = {}
    market_ids = sorted({c["market_id"] for c in enriched})
    with httpx.Client() as client:
        for mid in market_ids:
            outcome, _ = fetch_outcome(mid, client)
            if outcome:
                outcomes[mid] = outcome

    # Compute held-to-expiry PnL per position
    for c in enriched:
        outcome = outcomes.get(c["market_id"])
        if not outcome or c["entry_price"] <= 0:
            c["hold_pnl"] = None
            continue
        shares = c["size_usd"] / c["entry_price"]
        won = (outcome == c["side"])
        c["hold_pnl"] = shares * ((1.0 if won else 0.0) - c["entry_price"])
        c["won"] = won

    valid = [c for c in enriched if c.get("hold_pnl") is not None]
    print(f"  resolved: {len(valid)}/{len(enriched)}")
    print()

    # Bucket by edge
    print("=== Held-to-expiry PnL bucketed by entry edge ===")
    print(f"{'edge_bucket':>15} {'n':>4} {'wr':>6} {'pnl':>8} {'avg_pnl':>8}")
    edge_buckets = [(0.0, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 1.0)]
    for lo, hi in edge_buckets:
        in_bucket = [c for c in valid if lo <= c["entry_edge"] < hi]
        if not in_bucket:
            continue
        n = len(in_bucket)
        wins = sum(1 for c in in_bucket if c["won"])
        pnl = sum(c["hold_pnl"] for c in in_bucket)
        print(f"  [{lo:.2f}, {hi:.2f}) {n:4d} {wins/n*100:5.1f}% {pnl:+7.2f} {pnl/n:+7.2f}")

    print()
    print("=== Held-to-expiry PnL bucketed by entry confidence ===")
    print(f"{'conf_bucket':>15} {'n':>4} {'wr':>6} {'pnl':>8} {'avg_pnl':>8}")
    conf_buckets = [(0.0, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.95), (0.95, 1.01)]
    for lo, hi in conf_buckets:
        in_bucket = [c for c in valid if lo <= c["entry_confidence"] < hi]
        if not in_bucket:
            continue
        n = len(in_bucket)
        wins = sum(1 for c in in_bucket if c["won"])
        pnl = sum(c["hold_pnl"] for c in in_bucket)
        print(f"  [{lo:.2f}, {hi:.2f}) {n:4d} {wins/n*100:5.1f}% {pnl:+7.2f} {pnl/n:+7.2f}")

    print()
    print("=== Joint bucket: edge >= 0.15 AND confidence >= 0.75 ===")
    sel = [c for c in valid if c["entry_edge"] >= 0.15 and c["entry_confidence"] >= 0.75]
    if sel:
        wins = sum(1 for c in sel if c["won"])
        pnl = sum(c["hold_pnl"] for c in sel)
        print(f"  n={len(sel)}  wr={wins/len(sel)*100:.1f}%  pnl={pnl:+.2f}  avg={pnl/len(sel):+.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
