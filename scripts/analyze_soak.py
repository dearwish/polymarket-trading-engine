#!/usr/bin/env python3
"""Paper-soak analysis: correlate daemon_tick decisions against market resolution.

Usage:
    python scripts/analyze_soak.py                      # uses default paths
    python scripts/analyze_soak.py --events logs/events.jsonl --min-ticks 5

Fetches current Polymarket market state to determine resolution outcome.
Run after the markets you soaked on have closed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TickRecord:
    logged_at: str
    market_id: str
    question: str
    seconds_to_expiry: int
    suggested_side: str       # YES / NO / ABSTAIN
    fair_probability: float
    edge_yes: float
    edge_no: float
    confidence: float
    bid_yes: float
    ask_yes: float
    btc_price: float


@dataclass
class MarketSummary:
    market_id: str
    question: str
    ticks: list[TickRecord] = field(default_factory=list)
    outcome: str | None = None         # "YES" | "NO" | None (unresolved)
    final_implied: float | None = None


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------

def load_ticks(events_path: Path) -> dict[str, MarketSummary]:
    summaries: dict[str, MarketSummary] = {}
    with events_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event_type") != "daemon_tick":
                continue
            p = record.get("payload", {})
            market_id = str(p.get("market_id", ""))
            if not market_id:
                continue
            if market_id not in summaries:
                summaries[market_id] = MarketSummary(
                    market_id=market_id,
                    question=str(p.get("question", "")),
                )
            tick = TickRecord(
                logged_at=record.get("logged_at", ""),
                market_id=market_id,
                question=str(p.get("question", "")),
                seconds_to_expiry=int(p.get("seconds_to_expiry") or 0),
                suggested_side=str(p.get("suggested_side", "ABSTAIN")),
                fair_probability=float(p.get("fair_probability") or 0.5),
                edge_yes=float(p.get("edge_yes") or 0.0),
                edge_no=float(p.get("edge_no") or 0.0),
                confidence=float(p.get("confidence") or 0.0),
                bid_yes=float(p.get("bid_yes") or 0.0),
                ask_yes=float(p.get("ask_yes") or 0.0),
                btc_price=float(p.get("btc_price") or 0.0),
            )
            summaries[market_id].ticks.append(tick)
    return summaries


# ---------------------------------------------------------------------------
# Resolution fetching
# ---------------------------------------------------------------------------

def fetch_outcome(market_id: str, client: httpx.Client) -> tuple[str | None, float | None]:
    """Return (outcome, final_implied). outcome is 'YES'/'NO'/None."""
    try:
        r = client.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"  [warn] could not fetch market {market_id}: {exc}", file=sys.stderr)
        return None, None

    closed = bool(data.get("closed") or data.get("isResolved"))
    raw_prices = data.get("outcomePrices")
    if isinstance(raw_prices, str):
        try:
            raw_prices = json.loads(raw_prices)
        except Exception:
            raw_prices = None

    if isinstance(raw_prices, list) and len(raw_prices) >= 2:
        try:
            yes_price = float(raw_prices[0])
            final_implied = yes_price
        except (TypeError, ValueError):
            yes_price = None
            final_implied = None
    else:
        yes_price = None
        final_implied = float(data.get("outcomePrices", [0.5])[0]) if raw_prices else None

    if closed and yes_price is not None:
        if yes_price >= 0.99:
            return "YES", yes_price
        elif yes_price <= 0.01:
            return "NO", yes_price

    return None, final_implied  # unresolved or ambiguous


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def brier_score(predictions: list[float], outcomes: list[float]) -> float:
    if not predictions:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def analyze(summaries: dict[str, MarketSummary], min_ticks: int) -> None:
    client = httpx.Client(timeout=15)

    print("\n=== Fetching market resolutions ===")
    for ms in summaries.values():
        outcome, final_implied = fetch_outcome(ms.market_id, client)
        ms.outcome = outcome
        ms.final_implied = final_implied
        status = f"→ {outcome}" if outcome else "(unresolved)"
        print(f"  {ms.market_id}: {ms.question[:55]:55s}  {status}  implied={final_implied}")

    resolved = {mid: ms for mid, ms in summaries.items() if ms.outcome is not None}
    print(f"\n{len(resolved)}/{len(summaries)} markets resolved")

    if not resolved:
        print("No resolved markets — run again after markets close.")
        return

    print("\n=== Per-market breakdown ===")
    all_correct: list[bool] = []
    all_fair: list[float] = []
    all_outcomes: list[float] = []
    all_edges: list[float] = []
    abstain_ticks = 0
    total_ticks = 0

    for mid, ms in resolved.items():
        ticks = [t for t in ms.ticks if len(ms.ticks) >= min_ticks]
        if not ticks:
            continue

        outcome_bool = 1.0 if ms.outcome == "YES" else 0.0
        non_abstain = [t for t in ticks if t.suggested_side != "ABSTAIN"]
        abstain = [t for t in ticks if t.suggested_side == "ABSTAIN"]
        abstain_ticks += len(abstain)
        total_ticks += len(ticks)

        correct = [t for t in non_abstain if t.suggested_side == ms.outcome]
        hit_rate = len(correct) / len(non_abstain) if non_abstain else float("nan")

        avg_fair = sum(t.fair_probability for t in ticks) / len(ticks)
        avg_edge = sum(
            t.edge_yes if t.suggested_side == "YES" else t.edge_no
            for t in non_abstain
        ) / len(non_abstain) if non_abstain else float("nan")

        all_correct.extend([t.suggested_side == ms.outcome for t in non_abstain])
        all_fair.extend(t.fair_probability for t in ticks)
        all_outcomes.extend([outcome_bool] * len(ticks))
        all_edges.extend(
            t.edge_yes if t.suggested_side == "YES" else t.edge_no
            for t in non_abstain
        )

        hit_str = f"{hit_rate:.0%}" if not math.isnan(hit_rate) else "n/a"
        edge_str = f"{avg_edge:+.4f}" if not math.isnan(avg_edge) else "n/a"
        print(
            f"  {ms.question[:50]:50s}  outcome={ms.outcome}  "
            f"ticks={len(ticks):4d}  abstain={len(abstain):3d}  "
            f"hit={hit_str}  avg_fair={avg_fair:.3f}  avg_edge={edge_str}"
        )

    print("\n=== Aggregate stats ===")
    overall_hit = sum(all_correct) / len(all_correct) if all_correct else float("nan")
    bs = brier_score(all_fair, all_outcomes)
    avg_edge_all = sum(all_edges) / len(all_edges) if all_edges else float("nan")
    abstain_rate = abstain_ticks / total_ticks if total_ticks else float("nan")

    print(f"  Total ticks logged : {total_ticks}")
    print(f"  Abstain rate       : {abstain_rate:.1%}")
    print(f"  Non-abstain hit    : {overall_hit:.1%}" if not math.isnan(overall_hit) else "  Non-abstain hit    : n/a")
    print(f"  Mean edge (chosen) : {avg_edge_all:+.4f}" if not math.isnan(avg_edge_all) else "  Mean edge (chosen) : n/a")
    print(f"  Brier score (fair) : {bs:.4f}  (0=perfect, 0.25=random)")

    print()
    if not math.isnan(overall_hit) and overall_hit > 0.55 and not math.isnan(avg_edge_all) and avg_edge_all > 0.0:
        print("  ✓  Hit rate > 55% and mean edge > 0 — model looks viable")
    elif not math.isnan(overall_hit) and overall_hit > 0.5:
        print("  ~  Hit rate marginally positive — more data needed")
    else:
        print("  ✗  Hit rate ≤ 50% — model needs re-calibration before live trading")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events",
        default="logs/events.jsonl",
        help="Path to events.jsonl (default: logs/events.jsonl)",
    )
    parser.add_argument(
        "--min-ticks",
        type=int,
        default=3,
        help="Minimum daemon_tick count to include a market (default: 3)",
    )
    args = parser.parse_args()

    events_path = Path(args.events)
    if not events_path.exists():
        print(f"Events file not found: {events_path}", file=sys.stderr)
        sys.exit(1)

    summaries = load_ticks(events_path)
    print(f"Loaded {sum(len(ms.ticks) for ms in summaries.values())} daemon_tick events across {len(summaries)} markets")

    if not summaries:
        print("No daemon_tick events found in journal.")
        sys.exit(0)

    analyze(summaries, min_ticks=args.min_ticks)


if __name__ == "__main__":
    main()
