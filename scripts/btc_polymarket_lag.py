#!/usr/bin/env python3
"""Measure the price-lead relationship between Binance BTC and Polymarket.

Thesis: Polymarket BTC Up/Down markets are a derivative of the Binance
spot price. If Binance leads (trades set the price, then Polymarket
makers re-price after a round trip), there's a measurable lag — and
potentially an actionable one if our end-to-end latency is shorter
than the market's.

Method:
  1. Load daemon_tick events (which carry both ``btc_price`` and
     ``mid_yes`` at the same logged_at).
  2. For each market, sort ticks by time and compute consecutive
     returns:
        btc_return  = ln(btc_price[t] / btc_price[t-1])
        poly_return = ln(mid_yes[t]  / mid_yes[t-1])
  3. Cross-correlate the two series at integer tick lags ∈ [-5, +5].
     Each tick is ~1s in production (decision_min_interval_seconds),
     so a +k lag means "mid follows btc by k ticks" ≈ "k seconds".
  4. Report peak correlation and its lag per market, plus the overall
     winning lag across markets.

Limitations:
  - daemon_tick samples at decision cadence (~1/sec) so sub-second
    lag isn't resolvable. For finer granularity we'd need the raw
    binance/polymarket WS event streams, which aren't persisted.
  - mid_yes includes maker spread — a leading indicator would actually
    move the ask/bid first, then mid. Peak correlation on mid gives an
    upper-bound estimate of the lag, not a lower bound.

Usage:
    python scripts/btc_polymarket_lag.py
    python scripts/btc_polymarket_lag.py --events logs/backups/<…>/events.jsonl
    python scripts/btc_polymarket_lag.py --max-lag 10 --min-samples 200
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


def _parse_ts(s: str) -> float:
    """Parse ISO8601 → epoch seconds (float)."""
    # fromisoformat is ~3x faster than dateutil and sufficient for our
    # own-produced timestamps which always include tz offset or Z.
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _load_series(
    events_path: Path,
    cutoff_ts: float | None = None,
) -> dict[str, list[tuple[float, float, float]]]:
    """Return ``{market_id: [(t, btc_price, mid_yes), ...]}``.

    Only keeps ticks where both prices are populated and positive —
    cold-start rows with ``mid_yes == 0`` would produce ``-inf`` log
    returns and contaminate the correlation. When ``cutoff_ts`` is set
    (``time.time() - minutes * 60``), earlier ticks are skipped so the
    analysis windows down to the most recent ``minutes`` of wall time.
    """
    by_market: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
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
            btc = p.get("btc_price")
            mid = p.get("mid_yes")
            if not market_id or not btc or not mid:
                continue
            try:
                btc = float(btc)
                mid = float(mid)
            except (TypeError, ValueError):
                continue
            if btc <= 0 or mid <= 0:
                continue
            t = _parse_ts(rec.get("logged_at", ""))
            if cutoff_ts is not None and t < cutoff_ts:
                continue
            # Dedupe per (strategy_id, market_id, logged_at) — three
            # scorers emit at the same timestamp, so keep whichever row
            # arrives first and skip duplicates.
            if by_market[market_id] and by_market[market_id][-1][0] == t:
                continue
            by_market[market_id].append((t, btc, mid))
    for mid_key in by_market:
        by_market[mid_key].sort(key=lambda row: row[0])
    return by_market


def _log_returns(series: list[float]) -> list[float]:
    """Consecutive log returns; first element has no predecessor so the
    returned list has length ``len(series) - 1``."""
    out: list[float] = []
    for i in range(1, len(series)):
        prev = series[i - 1]
        curr = series[i]
        if prev <= 0 or curr <= 0:
            out.append(0.0)
            continue
        out.append(math.log(curr / prev))
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation. Returns 0.0 for zero-variance inputs rather
    than raising — a flat series correlates with nothing, not NaN.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return num / math.sqrt(vx * vy)


def _cross_corr(x: list[float], y: list[float], lag: int) -> float:
    """corr(x[t], y[t + lag]).

    Positive lag → y follows x (y at t+lag correlated with x at t), so
    a peak at positive lag means "mid follows btc" — Binance leads.
    """
    if lag == 0:
        return _pearson(x, y)
    if lag > 0:
        return _pearson(x[:-lag], y[lag:])
    return _pearson(x[-lag:], y[:lag])


def _analyze_market(series: list[tuple[float, float, float]], max_lag: int) -> dict:
    if len(series) < 3:
        return {"skipped": True, "reason": f"n={len(series)}"}
    btc = [row[1] for row in series]
    mid = [row[2] for row in series]
    # Median tick spacing — if it's ≫1s the "lag in seconds" interpretation breaks.
    spacings = [series[i][0] - series[i - 1][0] for i in range(1, len(series))]
    median_dt = statistics.median(spacings) if spacings else 0.0
    btc_ret = _log_returns(btc)
    mid_ret = _log_returns(mid)
    if len(btc_ret) < max_lag * 2:
        return {"skipped": True, "reason": f"n_ret={len(btc_ret)}"}
    lags = list(range(-max_lag, max_lag + 1))
    corrs = {lag: _cross_corr(btc_ret, mid_ret, lag) for lag in lags}
    peak_lag = max(lags, key=lambda lag: abs(corrs[lag]))
    return {
        "skipped": False,
        "n_ticks": len(series),
        "median_dt_s": median_dt,
        "peak_lag_ticks": peak_lag,
        "peak_corr": corrs[peak_lag],
        "corr_at_lag": corrs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", default="logs/events.jsonl", help="Events file")
    parser.add_argument(
        "--max-lag",
        type=int,
        default=5,
        help="Max lag in ticks to probe (default ±5; each tick ≈ decision cadence)",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=50,
        help="Skip markets with fewer ticks than this (default 50)",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=5.0,
        help="Only analyse ticks from the last N minutes (default 5). Use 0 for the whole file.",
    )
    args = parser.parse_args()

    events_path = Path(args.events)
    if not events_path.exists():
        raise SystemExit(f"events file not found: {events_path}")

    import time as _time
    cutoff: float | None = None
    if args.minutes > 0:
        cutoff = _time.time() - args.minutes * 60.0
        from datetime import datetime, timezone
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        print(f"Window: last {args.minutes:g} min (since {cutoff_iso})")

    print(f"Reading {events_path} …")
    by_market = _load_series(events_path, cutoff_ts=cutoff)
    print(f"Loaded ticks for {len(by_market)} markets")

    print(f"\n{'market':<12} {'n':>5}  {'Δt(s)':>6}  {'peak_lag':>8}  {'peak_corr':>9}")
    lag_votes: dict[int, int] = defaultdict(int)
    peak_corrs: list[float] = []
    for market_id, series in sorted(by_market.items()):
        if len(series) < args.min_samples:
            continue
        result = _analyze_market(series, args.max_lag)
        if result["skipped"]:
            continue
        lag = result["peak_lag_ticks"]
        corr = result["peak_corr"]
        lag_votes[lag] += 1
        peak_corrs.append(corr)
        print(
            f"{market_id:<12} {result['n_ticks']:>5}  "
            f"{result['median_dt_s']:>6.2f}  "
            f"{lag:>+8d}  {corr:>+9.3f}"
        )

    if not lag_votes:
        print("\nNo markets with enough samples. Try --min-samples 50.")
        return

    print("\n=== Aggregate ===")
    total = sum(lag_votes.values())
    for lag in sorted(lag_votes):
        pct = lag_votes[lag] / total * 100
        bar = "█" * int(pct / 2)
        marker = "  ← Binance leads" if lag > 0 else "  ← Polymarket leads" if lag < 0 else "  ← synchronous"
        print(f"  lag={lag:>+d}  n={lag_votes[lag]:>3}  ({pct:>4.1f}%)  {bar}{marker}")

    avg_peak = statistics.fmean(peak_corrs)
    median_peak = statistics.median(peak_corrs)
    print(f"\n  peak |corr|  avg={avg_peak:+.3f}  median={median_peak:+.3f}")
    print(f"  (values < 0.05 suggest no measurable lead-lag in this window.)")


if __name__ == "__main__":
    main()
