#!/usr/bin/env python3
"""Paper-soak analysis: correlate daemon_tick decisions against market resolution.

Usage:
    python scripts/analyze_soak.py                      # uses default paths
    python scripts/analyze_soak.py --events logs/events.jsonl --min-ticks 5
    python scripts/analyze_soak.py --shadow             # include retro-shadow comparison

Fetches current Polymarket market state to determine resolution outcome.
Run after the markets you soaked on have closed.

With --shadow the script re-simulates the htf_tilt shadow scorer offline
against every logged tick (using the already-stored btc_log_return_1h and
btc_session fields) and prints a side-by-side base vs shadow Brier / hit-rate
table so you can gate promotion without deploying the variant live.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    btc_session: str = "off"
    btc_log_return_1h: float = 0.0
    strategy_id: str = "fade"
    # Live shadow fields (present only when QUANT_SHADOW_VARIANT was set during soak)
    shadow_fair_probability: float | None = None
    shadow_suggested_side: str | None = None
    shadow_edge_yes: float | None = None
    shadow_edge_no: float | None = None


@dataclass
class MarketSummary:
    market_id: str
    question: str
    ticks: list[TickRecord] = field(default_factory=list)
    outcome: str | None = None         # "YES" | "NO" | None (unresolved)
    final_implied: float | None = None


@dataclass
class ClosedPositionRecord:
    """Subset of a position_closed event used by the hold-to-expiry analysis.

    Produced by the daemon's `_finalize_paper_close` helper; self-contained
    so analyze_soak can compare actual stop-out P&L against what would have
    been realized if the position had been held until market resolution.
    """
    market_id: str
    side: str            # "YES" | "NO"
    size_usd: float
    entry_price: float
    exit_price: float
    realized_pnl: float
    close_reason: str
    hold_seconds: float
    strategy_id: str = "fade"


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _in_window(ts_str: str, since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    dt = _parse_ts(ts_str)
    if dt is None:
        return True
    if since is not None and dt < since:
        return False
    if until is not None and dt > until:
        return False
    return True


def load_closed_positions(
    events_path: Path,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[ClosedPositionRecord]:
    """Parse position_closed events so --hold-to-expiry can attribute per-trade
    realized P&L to a side + entry price without a DB join."""
    out: list[ClosedPositionRecord] = []
    with events_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event_type") != "position_closed":
                continue
            if not _in_window(record.get("logged_at", ""), since, until):
                continue
            p = record.get("payload", {})
            try:
                out.append(
                    ClosedPositionRecord(
                        market_id=str(p.get("market_id", "")),
                        side=str(p.get("side", "")),
                        size_usd=float(p.get("size_usd") or 0.0),
                        entry_price=float(p.get("entry_price") or 0.0),
                        exit_price=float(p.get("exit_price") or 0.0),
                        realized_pnl=float(p.get("realized_pnl") or 0.0),
                        close_reason=str(p.get("close_reason", "")),
                        hold_seconds=float(p.get("hold_seconds") or 0.0),
                        strategy_id=str(p.get("strategy_id") or "fade"),
                    )
                )
            except (TypeError, ValueError):
                continue
    return out


def load_ticks(
    events_path: Path,
    since: datetime | None = None,
    until: datetime | None = None,
    strategy_id: str | None = None,
) -> dict[str, MarketSummary]:
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
            if not _in_window(record.get("logged_at", ""), since, until):
                continue
            p = record.get("payload", {})
            market_id = str(p.get("market_id", ""))
            if not market_id:
                continue
            tick_strategy = str(p.get("strategy_id") or "fade")
            if strategy_id is not None and tick_strategy != strategy_id:
                continue
            if market_id not in summaries:
                summaries[market_id] = MarketSummary(
                    market_id=market_id,
                    question=str(p.get("question", "")),
                )
            shadow_side_raw = p.get("shadow_suggested_side")
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
                btc_session=str(p.get("btc_session") or "off"),
                btc_log_return_1h=float(p.get("btc_log_return_1h") or 0.0),
                strategy_id=tick_strategy,
                shadow_fair_probability=float(p["shadow_fair_probability"]) if p.get("shadow_fair_probability") is not None else None,
                shadow_suggested_side=str(shadow_side_raw) if shadow_side_raw is not None else None,
                shadow_edge_yes=float(p["shadow_edge_yes"]) if p.get("shadow_edge_yes") is not None else None,
                shadow_edge_no=float(p["shadow_edge_no"]) if p.get("shadow_edge_no") is not None else None,
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


def _retro_shadow_side(
    tick: TickRecord,
    htf_tilt_strength: float,
    session_bias_eu: float,
    session_bias_us: float,
) -> tuple[str, float]:
    """Re-simulate the htf_tilt shadow scorer from logged tick fields.

    Returns (suggested_side, shadow_fair_probability).
    """
    base_fair = tick.fair_probability
    tilt = 0.0
    if tick.btc_log_return_1h != 0.0:
        tilt += (1.0 if tick.btc_log_return_1h > 0.0 else -1.0) * htf_tilt_strength
    biases = {"eu": session_bias_eu, "us": session_bias_us}
    tilt += biases.get(tick.btc_session, 0.0)
    fair_yes = max(0.01, min(0.99, base_fair + tilt))
    # Reproduce edge signs using the same ask prices the live scorer saw.
    # We don't have slippage/fee here so we use a zero-cost approximation;
    # the sign of chosen side is what matters for hit-rate comparison.
    edge_yes = fair_yes - tick.ask_yes
    edge_no = (1.0 - fair_yes) - (1.0 - tick.bid_yes)  # bid_yes ≈ ask_no complement
    if edge_yes <= 0.0 and edge_no <= 0.0:
        return "ABSTAIN", fair_yes
    if edge_yes >= edge_no:
        return "YES", fair_yes
    return "NO", fair_yes


def _print_scorer_stats(
    label: str,
    resolved: dict[str, "MarketSummary"],
    get_side: "Callable[[TickRecord], str | None]",
    get_fair: "Callable[[TickRecord], float]",
    min_ticks: int,
) -> None:
    all_correct: list[bool] = []
    all_fair: list[float] = []
    all_outcomes: list[float] = []
    session_correct: dict[str, list[bool]] = defaultdict(list)

    for ms in resolved.values():
        ticks = [t for t in ms.ticks if len(ms.ticks) >= min_ticks]
        if not ticks:
            continue
        outcome_bool = 1.0 if ms.outcome == "YES" else 0.0
        for t in ticks:
            side = get_side(t)
            if side is None or side == "ABSTAIN":
                continue
            correct = side == ms.outcome
            all_correct.append(correct)
            all_fair.append(get_fair(t))
            all_outcomes.append(outcome_bool)
            session_correct[t.btc_session].append(correct)

    overall_hit = sum(all_correct) / len(all_correct) if all_correct else float("nan")
    bs = brier_score(all_fair, all_outcomes)
    print(f"\n  [{label}]")
    print(f"    Non-abstain ticks : {len(all_correct)}")
    hit_str = f"{overall_hit:.1%}" if not math.isnan(overall_hit) else "n/a"
    print(f"    Hit rate          : {hit_str}")
    print(f"    Brier score       : {bs:.4f}")
    for sess in ("asia", "eu", "us", "off"):
        data = session_correct.get(sess, [])
        if not data:
            continue
        h = sum(data) / len(data)
        print(f"    Session {sess:6s}    : {h:.1%}  (n={len(data)})")


def _hold_to_expiry_pnl(pos: ClosedPositionRecord, outcome: str) -> float:
    """P&L had the position been held to resolution instead of stopped out.

    YES outcome pays the YES-token holder $1; NO pays the NO-token holder $1.
    So for a YES position the resolution price is 1 if outcome=YES else 0, and
    analogously for NO. Uses the same formula as PortfolioEngine._compute_pnl.
    """
    if pos.entry_price <= 0 or pos.size_usd <= 0:
        return 0.0
    if pos.side == "YES":
        resolution_price = 1.0 if outcome == "YES" else 0.0
    elif pos.side == "NO":
        resolution_price = 1.0 if outcome == "NO" else 0.0
    else:
        return 0.0
    shares = pos.size_usd / pos.entry_price
    return (resolution_price - pos.entry_price) * shares


def analyze_hold_to_expiry(
    closed: list[ClosedPositionRecord],
    resolved: dict[str, MarketSummary],
) -> None:
    """Print actual (stopped) P&L vs hypothetical hold-to-expiry P&L.

    Only counts positions whose market fetched a resolved YES/NO outcome.
    The delta is the narrow answer to "are our stops destroying edge?"
    """
    matched: list[tuple[ClosedPositionRecord, str]] = []
    for pos in closed:
        ms = resolved.get(pos.market_id)
        if ms is None or ms.outcome is None:
            continue
        matched.append((pos, ms.outcome))

    print("\n=== Hold-to-expiry counterfactual ===")
    if not matched:
        print("  No resolved closed positions in the event log — try again after markets resolve.")
        return

    actual_total = 0.0
    hold_total = 0.0
    correct_side = 0
    by_reason: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for pos, outcome in matched:
        hold_pnl = _hold_to_expiry_pnl(pos, outcome)
        actual_total += pos.realized_pnl
        hold_total += hold_pnl
        if (pos.side == "YES" and outcome == "YES") or (pos.side == "NO" and outcome == "NO"):
            correct_side += 1
        by_reason[pos.close_reason].append((pos.realized_pnl, hold_pnl))

    n = len(matched)
    win_rate = correct_side / n if n else 0.0
    print(f"  Matched trades        : {n}")
    print(f"  Side-correct (to expiry): {correct_side}/{n} ({win_rate:.1%})")
    print(f"  Actual stopped P&L    : {actual_total:+.4f}")
    print(f"  Hold-to-expiry P&L    : {hold_total:+.4f}")
    print(f"  Delta (hold − stop)   : {(hold_total - actual_total):+.4f}")

    print("  By close reason:")
    for reason in sorted(by_reason):
        rows = by_reason[reason]
        a = sum(r[0] for r in rows)
        h = sum(r[1] for r in rows)
        print(f"    {reason:25s}  n={len(rows):3d}  stopped={a:+.4f}  hold={h:+.4f}  delta={h - a:+.4f}")

    if hold_total > actual_total + 1e-6:
        print("\n  ▶ Stops appear to be cutting winners short — widen or switch to time-based exit.")
    elif hold_total < actual_total - 1e-6:
        print("\n  ▶ Stops are protecting capital — hold-to-expiry would lose more than current exits.")
    else:
        print("\n  ▶ Stops and expiry are roughly equivalent in this sample.")


def _print_strategy_breakdown(closed_positions: list[ClosedPositionRecord]) -> None:
    """Side-by-side PnL / win-rate stats per strategy_id from position_closed
    events. Skips the section when only one strategy is present (the numbers
    would just duplicate the aggregate view).
    """
    if not closed_positions:
        return
    by_strategy: dict[str, list[ClosedPositionRecord]] = defaultdict(list)
    for pos in closed_positions:
        by_strategy[pos.strategy_id].append(pos)
    if len(by_strategy) < 2:
        return

    print("\n=== Per-strategy breakdown (closed positions) ===")
    print(
        f"  {'strategy':<14s}  {'n':>4s}  {'total_pnl':>10s}  "
        f"{'win_rate':>8s}  {'avg_hold_s':>10s}  {'avg_pnl':>8s}"
    )
    for strategy_id in sorted(by_strategy):
        rows = by_strategy[strategy_id]
        n = len(rows)
        total_pnl = sum(r.realized_pnl for r in rows)
        wins = sum(1 for r in rows if r.realized_pnl > 0.0)
        win_rate = wins / n if n else float("nan")
        avg_hold = sum(r.hold_seconds for r in rows) / n if n else 0.0
        avg_pnl = total_pnl / n if n else 0.0
        print(
            f"  {strategy_id:<14s}  {n:>4d}  {total_pnl:>+10.4f}  "
            f"{win_rate:>8.1%}  {avg_hold:>10.1f}  {avg_pnl:>+8.4f}"
        )


def analyze(
    summaries: dict[str, MarketSummary],
    min_ticks: int,
    shadow: bool = False,
    htf_tilt_strength: float = 0.10,
    session_bias_eu: float = 0.0,
    session_bias_us: float = 0.0,
    closed_positions: list[ClosedPositionRecord] | None = None,
    hold_to_expiry: bool = False,
) -> None:
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

    if closed_positions is not None:
        _print_strategy_breakdown(closed_positions)

    if hold_to_expiry and closed_positions is not None:
        analyze_hold_to_expiry(closed_positions, resolved)

    if not shadow:
        return

    # --- Retro-shadow comparison -------------------------------------------------
    print("\n=== Retro-shadow A/B (base vs htf_tilt) ===")
    print(f"    HTF tilt strength : {htf_tilt_strength:+.3f}")
    print(f"    Session bias EU   : {session_bias_eu:+.3f}")
    print(f"    Session bias US   : {session_bias_us:+.3f}")

    # Base scorer (using logged fair_probability and suggested_side)
    _print_scorer_stats(
        "base",
        resolved,
        get_side=lambda t: t.suggested_side,
        get_fair=lambda t: t.fair_probability,
        min_ticks=min_ticks,
    )

    # Live shadow (only ticks where the daemon already logged shadow fields)
    live_shadow_ticks = sum(
        1 for ms in resolved.values() for t in ms.ticks if t.shadow_suggested_side is not None
    )
    if live_shadow_ticks > 0:
        _print_scorer_stats(
            f"shadow-live ({live_shadow_ticks} ticks)",
            resolved,
            get_side=lambda t: t.shadow_suggested_side,
            get_fair=lambda t: t.shadow_fair_probability if t.shadow_fair_probability is not None else t.fair_probability,
            min_ticks=min_ticks,
        )

    # Retro-simulated shadow (re-applies tilt offline to all ticks)
    def retro_side(t: TickRecord) -> str:
        side, _ = _retro_shadow_side(t, htf_tilt_strength, session_bias_eu, session_bias_us)
        return side

    def retro_fair(t: TickRecord) -> float:
        _, fp = _retro_shadow_side(t, htf_tilt_strength, session_bias_eu, session_bias_us)
        return fp

    _print_scorer_stats(
        "shadow-retro (all ticks)",
        resolved,
        get_side=retro_side,
        get_fair=retro_fair,
        min_ticks=min_ticks,
    )
    print()


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
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Print retro-shadow A/B comparison (base vs htf_tilt variant)",
    )
    parser.add_argument(
        "--hold-to-expiry",
        action="store_true",
        help="Compare actual stopped P&L against hypothetical hold-to-resolution P&L",
    )
    parser.add_argument(
        "--htf-tilt-strength",
        type=float,
        default=0.10,
        help="HTF tilt magnitude for retro-shadow (default: 0.10)",
    )
    parser.add_argument(
        "--session-bias-eu",
        type=float,
        default=0.0,
        help="Additive EU-session bias for retro-shadow (default: 0.0)",
    )
    parser.add_argument(
        "--session-bias-us",
        type=float,
        default=0.0,
        help="Additive US-session bias for retro-shadow (default: 0.0)",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help=(
            "Only include ticks whose payload strategy_id matches. "
            "Use to drill into one scorer when multiple run side-by-side "
            "(e.g. --strategy adaptive). Leave unset to load all strategies."
        ),
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only include ticks logged at or after this ISO timestamp (e.g. 2026-04-21T05:52Z)",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Only include ticks logged at or before this ISO timestamp (e.g. 2026-04-21T09:59Z)",
    )
    parser.add_argument(
        "--settings-timeline",
        action="store_true",
        help="Print chronological settings_changes from the DB and exit",
    )
    parser.add_argument(
        "--db",
        default="data/agent.db",
        help="Path to the soak DB for --settings-timeline (default: data/agent.db)",
    )
    args = parser.parse_args()

    if args.settings_timeline:
        _print_settings_timeline(Path(args.db))
        return

    since: datetime | None = _parse_ts(args.since) if args.since else None
    until: datetime | None = _parse_ts(args.until) if args.until else None
    if since and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    events_path = Path(args.events)
    if not events_path.exists():
        print(f"Events file not found: {events_path}", file=sys.stderr)
        sys.exit(1)

    window_note = ""
    if since or until:
        window_note = f"  window: {since.isoformat() if since else '—'} → {until.isoformat() if until else '—'}\n"

    summaries = load_ticks(events_path, since=since, until=until, strategy_id=args.strategy)
    tick_strategies = {
        tick.strategy_id for ms in summaries.values() for tick in ms.ticks
    }
    total_ticks = sum(len(ms.ticks) for ms in summaries.values())
    strategy_note = ""
    if args.strategy:
        strategy_note = f" [strategy={args.strategy}]"
    elif len(tick_strategies) > 1:
        strategy_note = f" [strategies={','.join(sorted(tick_strategies))}]"
    print(
        f"Loaded {total_ticks} daemon_tick events across {len(summaries)} markets{strategy_note}"
    )
    if window_note:
        print(window_note, end="")
    if not args.strategy and len(tick_strategies) > 1:
        print(
            "  [warn] ticks span multiple strategies — scorer stats blend them. "
            "Pass --strategy <id> to isolate one.",
            file=sys.stderr,
        )

    # Always load closed positions — the per-strategy breakdown runs
    # whenever multiple strategies emitted closes, not only on --hold-to-expiry.
    closed_positions = load_closed_positions(events_path, since=since, until=until)
    if args.hold_to_expiry or any(p.strategy_id != "fade" for p in closed_positions):
        print(f"Loaded {len(closed_positions)} position_closed events")

    if not summaries:
        print("No daemon_tick events found in journal.")
        sys.exit(0)

    analyze(
        summaries,
        min_ticks=args.min_ticks,
        shadow=args.shadow,
        htf_tilt_strength=args.htf_tilt_strength,
        session_bias_eu=args.session_bias_eu,
        session_bias_us=args.session_bias_us,
        closed_positions=closed_positions,
        hold_to_expiry=args.hold_to_expiry,
    )


def _print_settings_timeline(db_path: Path) -> None:
    """Print the full contents of ``settings_changes`` from ``db_path``.

    Queries the DB directly (not via ``SettingsStore``) so this script stays
    usable against a backup DB without importing the rest of the package.
    Backup DBs are the intended A/B-analysis artefact — being able to point
    this at any ``agent.db`` snapshot is the whole reason the history table
    exists.
    """
    import sqlite3

    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT id, changed_at, field, value_before, value_after, source, reason "
                "FROM settings_changes ORDER BY id ASC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"Cannot read settings_changes from {db_path}: {exc}", file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()
    if not rows:
        print(f"No settings_changes rows in {db_path}.")
        return
    print(f"\n=== Settings timeline ({db_path}) ===")
    print(f"{'id':>5s}  {'changed_at':26s}  {'source':10s}  {'field':38s}  before → after  reason")
    for row in rows:
        id_, changed_at, field, before, after, source, reason = row
        def _fmt(raw: Any) -> str:
            if raw is None:
                return "—"
            try:
                return json.dumps(json.loads(raw))
            except (TypeError, json.JSONDecodeError):
                return str(raw)
        print(
            f"{id_:>5d}  {str(changed_at):26s}  {str(source):10s}  "
            f"{field:38s}  {_fmt(before)} → {_fmt(after)}  "
            f"{reason or ''}"
        )


if __name__ == "__main__":
    main()
