"""Backtest the gabagool hedged-pair thesis on existing soak data.

Mechanic under test: post passive maker bids on BOTH the YES and NO
tokens of the same binary market, offset one tick below the visible
ask on each side. If BOTH legs fill, hold to resolution and collect
``$1.00 − (fill_price_yes + fill_price_no)`` minus Polymarket's 2%
fee on profit. If only one leg fills within ``maker_ttl_seconds``,
cancel the unfilled side and flat-exit the filled leg at the next
bid (conservative — models the single-leg-exposure failure mode).

Fill model: same conservative ``maker_through`` rule as the penny
maker backtest — a later tick must show the ask DROPPING BELOW our
resting bid before we count the leg as filled. Touch fills don't
count because we cannot know our queue position.

Signal rule: enter on the first tick per market where
``(ask_yes − maker_offset) + (ask_no − maker_offset) ≤ pair_cost_max``
AND ``seconds_to_expiry ≥ min_tte_seconds``. ``pair_cost_max``
should bake in the Polymarket profit fee: a target of ``0.97``
implies ~3¢ pre-fee discount, ~1¢ after the 2% take.

Hold-to-resolution exit uses ``scripts/analyze_soak.py``'s
gamma-api.polymarket.com lookup, cached to
``scripts/_hedged_pair_resolution_cache.json`` so reruns are fast.

Reuses ``Tick`` + ``load_ticks`` + ``detect_files`` from
``penny_pattern_mining.py`` so the candidate universe stays in sync
with upstream research.

Phase-1 kill criterion (per plan): if net P&L after fees is below the
existing penny strategy's realized P&L on the same window, the
hedged-pair build is not pursued.

Usage:
  uv run python scripts/hedged_pair_backtest.py
  uv run python scripts/hedged_pair_backtest.py --pair-cost-max 0.98 --maker-offset 0.01
  uv run python scripts/hedged_pair_backtest.py --days 14 --no-fetch-resolutions
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Reuse upstream tick loader + dataclass so the candidate universe
# stays consistent with penny_pattern_mining / penny_maker_backtest.
sys.path.insert(0, str(Path(__file__).parent))
from penny_pattern_mining import (  # noqa: E402
    Tick,
    detect_files,
    load_ticks,
)


RESOLUTION_CACHE_PATH = Path("scripts/_hedged_pair_resolution_cache.json")


@dataclass
class PairTrade:
    market_id: str
    slug: str
    entry_signal_ts: float
    pair_cost_at_signal: float
    seconds_to_expiry_at_signal: int

    # Per-leg fills
    yes_maker_bid: float
    no_maker_bid: float
    yes_fill_ts: float | None = None
    yes_fill_price: float | None = None
    no_fill_ts: float | None = None
    no_fill_price: float | None = None

    # Sizing (shares per leg). When asymmetric, the cheaper leg's
    # dollar allocation is biased UP so the share counts are closer.
    yes_size_usd: float = 0.0
    no_size_usd: float = 0.0
    yes_shares: float = 0.0
    no_shares: float = 0.0

    # Outcome (hold-to-resolution path)
    outcome: str | None = None      # "YES" / "NO" / None (unresolved)
    resolution_source: str | None = None  # "api" / "log_final_tick" / "unresolved"

    # Single-leg flatten path (one fill, the other never came)
    flatten_ts: float | None = None
    flatten_price: float | None = None

    # P&L (USD, after fees)
    gross_pnl_usd: float = 0.0
    fee_paid_usd: float = 0.0
    net_pnl_usd: float = 0.0

    # Reason classification
    exit_reason: str = "pending"  # "both_filled_resolved" / "single_yes_flat" /
                                  # "single_no_flat" / "no_fill" / "unresolved" /
                                  # "no_lookahead"


# ---------------------------------------------------------------------------
# Resolution cache + fetch
# ---------------------------------------------------------------------------

def load_resolution_cache() -> dict[str, dict]:
    if RESOLUTION_CACHE_PATH.exists():
        try:
            return json.loads(RESOLUTION_CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_resolution_cache(cache: dict[str, dict]) -> None:
    RESOLUTION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESOLUTION_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def fetch_resolution(market_id: str, cache: dict[str, dict]) -> tuple[str | None, str]:
    """Return (outcome, source). outcome is 'YES'/'NO'/None; source is
    'api' or 'cache' (or 'error')."""
    if market_id in cache:
        return cache[market_id].get("outcome"), "cache"
    try:
        import httpx
        r = httpx.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"  [warn] could not fetch market {market_id}: {exc}", file=sys.stderr)
        cache[market_id] = {"outcome": None, "error": str(exc)}
        return None, "error"

    closed = bool(data.get("closed") or data.get("isResolved"))
    raw_prices = data.get("outcomePrices")
    if isinstance(raw_prices, str):
        try:
            raw_prices = json.loads(raw_prices)
        except Exception:
            raw_prices = None

    outcome: str | None = None
    if isinstance(raw_prices, list) and len(raw_prices) >= 2 and closed:
        try:
            yes_price = float(raw_prices[0])
            if yes_price >= 0.99:
                outcome = "YES"
            elif yes_price <= 0.01:
                outcome = "NO"
        except (TypeError, ValueError):
            pass

    cache[market_id] = {"outcome": outcome, "closed": closed, "raw_prices": raw_prices}
    return outcome, "api"


def infer_resolution_from_final_tick(mticks: list[Tick]) -> str | None:
    """Last-resort: read the final tick's bid_yes. If clearly resolved
    (>= 0.95 or <= 0.05), call it. Otherwise None."""
    if not mticks:
        return None
    last = mticks[-1]
    if last.bid_yes >= 0.95:
        return "YES"
    if last.bid_yes <= 0.05 and last.ask_yes <= 0.10:
        return "NO"
    return None


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def find_pair_signals(
    ticks: list[Tick],
    pair_cost_max: float,
    maker_offset: float,
    min_tte: int,
) -> list[tuple[list[Tick], int]]:
    """Return [(per_market_ticks, index)] — first eligible tick per market.

    Eligibility: a hypothetical pair of maker bids posted at
    ``ask − maker_offset`` on each side sums to ``≤ pair_cost_max``,
    AND TTE ≥ ``min_tte``. We only enter once per market — the entry
    is the first opportunity; subsequent ticks at the same eligibility
    are ignored (we'd be doubling down on a single market, which the
    plan does not yet support).
    """
    by_market: dict[str, list[Tick]] = defaultdict(list)
    for t in ticks:
        by_market[t.market_id].append(t)
    for mid in by_market:
        by_market[mid].sort(key=lambda t: t.ts)

    signals: list[tuple[list[Tick], int]] = []
    for mid, mticks in by_market.items():
        for i, t in enumerate(mticks):
            if t.seconds_to_expiry < min_tte:
                continue
            if t.ask_yes <= 0 or t.ask_no <= 0:
                continue
            yes_bid = round(t.ask_yes - maker_offset, 4)
            no_bid = round(t.ask_no - maker_offset, 4)
            if yes_bid <= 0 or no_bid <= 0:
                continue
            if yes_bid + no_bid > pair_cost_max:
                continue
            signals.append((mticks, i))
            break  # first opportunity per market only
    return signals


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_pair(
    mticks: list[Tick],
    i: int,
    maker_offset: float,
    maker_ttl_seconds: float,
    size_usd: float,
    asymmetric: bool,
    fee_rate: float,
    resolution: str | None,
    resolution_source: str,
) -> PairTrade:
    t = mticks[i]
    yes_bid = round(t.ask_yes - maker_offset, 4)
    no_bid = round(t.ask_no - maker_offset, 4)

    # Size split. Symmetric = $/2 each. Asymmetric biases dollars to
    # the cheaper leg so share counts are closer (article's "buy more
    # of whichever side is dipping"). Cap the asymmetric tilt at 70/30
    # so a degenerate book can't put 99% on one side.
    if asymmetric and (yes_bid > 0 and no_bid > 0):
        # Inverse-price weighting, clamped.
        inv_yes = 1.0 / yes_bid
        inv_no = 1.0 / no_bid
        w_yes = inv_yes / (inv_yes + inv_no)
        w_yes = max(0.30, min(0.70, w_yes))
        yes_size = size_usd * w_yes
        no_size = size_usd * (1 - w_yes)
    else:
        yes_size = size_usd / 2.0
        no_size = size_usd / 2.0

    pt = PairTrade(
        market_id=t.market_id,
        slug=getattr(t, "slug", "") or "",
        entry_signal_ts=t.ts,
        pair_cost_at_signal=yes_bid + no_bid,
        seconds_to_expiry_at_signal=t.seconds_to_expiry,
        yes_maker_bid=yes_bid,
        no_maker_bid=no_bid,
        yes_size_usd=yes_size,
        no_size_usd=no_size,
    )

    # Walk forward up to maker_ttl looking for maker-through fills on
    # each leg. Each side fills independently.
    end_wait = t.ts + maker_ttl_seconds
    for j in range(i + 1, len(mticks)):
        tj = mticks[j]
        if tj.ts > end_wait:
            break
        # YES leg
        if pt.yes_fill_ts is None and tj.ask_yes > 0 and tj.ask_yes < yes_bid:
            pt.yes_fill_ts = tj.ts
            pt.yes_fill_price = yes_bid
            pt.yes_shares = yes_size / yes_bid
        # NO leg
        if pt.no_fill_ts is None and tj.ask_no > 0 and tj.ask_no < no_bid:
            pt.no_fill_ts = tj.ts
            pt.no_fill_price = no_bid
            pt.no_shares = no_size / no_bid
        if pt.yes_fill_ts is not None and pt.no_fill_ts is not None:
            break

    # Branch on what filled.
    yes_filled = pt.yes_fill_ts is not None
    no_filled = pt.no_fill_ts is not None

    if not yes_filled and not no_filled:
        pt.exit_reason = "no_fill"
        return pt

    if yes_filled and no_filled:
        # Hedged pair locked in. Hold to resolution.
        return _resolve_pair(pt, mticks, resolution, resolution_source, fee_rate)

    # Single-leg fill. Flat-exit on the next bid after maker_ttl.
    return _flatten_single_leg(pt, mticks, i, maker_ttl_seconds, fee_rate)


def _resolve_pair(
    pt: PairTrade,
    mticks: list[Tick],
    resolution: str | None,
    resolution_source: str,
    fee_rate: float,
) -> PairTrade:
    """Both legs filled. Resolve to YES/NO and compute P&L."""
    if resolution is None:
        # Try last-tick inference; if still unresolved, mark unresolved.
        resolution = infer_resolution_from_final_tick(mticks)
        resolution_source = "log_final_tick" if resolution else "unresolved"

    pt.outcome = resolution
    pt.resolution_source = resolution_source

    if resolution is None:
        # Unresolved — can't book P&L. Treat as 0 for aggregate, flag.
        pt.exit_reason = "unresolved"
        return pt

    # YES outcome: YES shares pay $1, NO shares pay $0.
    # NO outcome:  YES shares pay $0, NO shares pay $1.
    if resolution == "YES":
        gross_pnl = (1.0 - pt.yes_fill_price) * pt.yes_shares - pt.no_fill_price * pt.no_shares
    else:  # NO
        gross_pnl = -pt.yes_fill_price * pt.yes_shares + (1.0 - pt.no_fill_price) * pt.no_shares

    fee = max(0.0, gross_pnl) * fee_rate
    pt.gross_pnl_usd = gross_pnl
    pt.fee_paid_usd = fee
    pt.net_pnl_usd = gross_pnl - fee
    pt.exit_reason = "both_filled_resolved"
    return pt


def _flatten_single_leg(
    pt: PairTrade,
    mticks: list[Tick],
    signal_idx: int,
    maker_ttl_seconds: float,
    fee_rate: float,
) -> PairTrade:
    """One leg filled, the other timed out. Flat-exit the filled leg at
    the next available bid after the TTL boundary."""
    flatten_after_ts = pt.entry_signal_ts + maker_ttl_seconds
    yes_filled = pt.yes_fill_ts is not None
    side = "YES" if yes_filled else "NO"
    pt.exit_reason = f"single_{side.lower()}_flat"

    for j in range(signal_idx + 1, len(mticks)):
        tj = mticks[j]
        if tj.ts < flatten_after_ts:
            continue
        exit_bid = tj.bid_yes if yes_filled else tj.bid_no
        if exit_bid <= 0:
            continue
        pt.flatten_ts = tj.ts
        pt.flatten_price = exit_bid
        if yes_filled:
            gross = (exit_bid - pt.yes_fill_price) * pt.yes_shares
        else:
            gross = (exit_bid - pt.no_fill_price) * pt.no_shares
        fee = max(0.0, gross) * fee_rate
        pt.gross_pnl_usd = gross
        pt.fee_paid_usd = fee
        pt.net_pnl_usd = gross - fee
        return pt

    # No flatten tick available — treat as held-to-expiry of the single
    # leg with worst-case 0 payout. Conservative.
    pt.exit_reason = "no_lookahead"
    if yes_filled:
        gross = -pt.yes_fill_price * pt.yes_shares
    else:
        gross = -pt.no_fill_price * pt.no_shares
    pt.gross_pnl_usd = gross
    pt.fee_paid_usd = 0.0
    pt.net_pnl_usd = gross
    return pt


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarize(trades: list[PairTrade]) -> str:
    n = len(trades)
    by_reason: dict[str, list[PairTrade]] = defaultdict(list)
    for tr in trades:
        by_reason[tr.exit_reason].append(tr)

    booked = [tr for tr in trades if tr.exit_reason in ("both_filled_resolved", "single_yes_flat", "single_no_flat", "no_lookahead")]
    pairs_filled = by_reason.get("both_filled_resolved", []) + by_reason.get("unresolved", [])
    paired_resolved = by_reason.get("both_filled_resolved", [])

    total_pnl = sum(tr.net_pnl_usd for tr in booked)
    total_gross = sum(tr.gross_pnl_usd for tr in booked)
    total_fee = sum(tr.fee_paid_usd for tr in booked)
    wins = [tr for tr in booked if tr.net_pnl_usd > 0]
    losses = [tr for tr in booked if tr.net_pnl_usd < 0]
    pair_wins = [tr for tr in paired_resolved if tr.net_pnl_usd > 0]

    out: list[str] = []
    out.append("| Metric | Value |")
    out.append("|---|---:|")
    out.append(f"| signals | {n} |")
    out.append(f"| both legs filled (pair locked) | {len(pairs_filled)} ({len(pairs_filled)/max(n,1):.1%}) |")
    out.append(f"| ...of which resolved YES/NO | {len(paired_resolved)} |")
    out.append(f"| single-leg only | {len(by_reason.get('single_yes_flat', [])) + len(by_reason.get('single_no_flat', []))} |")
    out.append(f"| no fill | {len(by_reason.get('no_fill', []))} |")
    out.append(f"| unresolved / no lookahead | {len(by_reason.get('unresolved', [])) + len(by_reason.get('no_lookahead', []))} |")
    out.append(f"| paired-trade win rate | {len(pair_wins)/max(len(paired_resolved),1):.1%} ({len(pair_wins)}/{len(paired_resolved)}) |")
    out.append(f"| overall win rate (all booked) | {len(wins)/max(len(booked),1):.1%} ({len(wins)}/{len(booked)}) |")
    out.append(f"| total gross P&L ($) | {total_gross:+.4f} |")
    out.append(f"| total Polymarket fees ($) | {total_fee:.4f} |")
    out.append(f"| **total net P&L ($)** | **{total_pnl:+.4f}** |")
    if paired_resolved:
        mean_pair_pnl = sum(tr.net_pnl_usd for tr in paired_resolved) / len(paired_resolved)
        out.append(f"| mean P&L per paired trade ($) | {mean_pair_pnl:+.4f} |")
    return "\n".join(out)


def write_report(
    args,
    trades: list[PairTrade],
    files: list[Path],
    out_path: Path,
) -> None:
    sections: list[str] = []
    sections.append("# Hedged-Pair (Gabagool) Backtest\n")
    sections.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}\n")
    sections.append("## Configuration\n")
    sections.append("| Setting | Value |")
    sections.append("|---|---|")
    sections.append(f"| `--days` | {args.days} |")
    sections.append(f"| `--pair-cost-max` | {args.pair_cost_max} |")
    sections.append(f"| `--maker-offset` | {args.maker_offset} |")
    sections.append(f"| `--maker-ttl-seconds` | {args.maker_ttl_seconds} |")
    sections.append(f"| `--min-tte` | {args.min_tte}s |")
    sections.append(f"| `--max-tte` | {args.max_tte}s |")
    sections.append(f"| `--size-usd` | ${args.size_usd} per pair |")
    sections.append(f"| `--asymmetric` | {args.asymmetric} |")
    sections.append(f"| `--fee-rate` | {args.fee_rate} |")
    sections.append(f"| `--fetch-resolutions` | {not args.no_fetch_resolutions} |")
    sections.append(f"| files scanned | {len(files)} |")
    sections.append("")
    sections.append("## Results\n")
    sections.append(summarize(trades))
    sections.append("\n## Per-trade detail\n")
    sections.append("| market_id | slug | pair@sig | tte@sig | yes_bid | no_bid | yes_fill | no_fill | outcome | reason | net_pnl |")
    sections.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|")
    for tr in sorted(trades, key=lambda t: t.entry_signal_ts):
        sections.append(
            f"| `{tr.market_id}` | `{tr.slug}` | {tr.pair_cost_at_signal:.4f} | {tr.seconds_to_expiry_at_signal} | "
            f"{tr.yes_maker_bid:.3f} | {tr.no_maker_bid:.3f} | "
            f"{(f'{tr.yes_fill_price:.3f}' if tr.yes_fill_price else '—')} | "
            f"{(f'{tr.no_fill_price:.3f}' if tr.no_fill_price else '—')} | "
            f"{tr.outcome or '—'} | {tr.exit_reason} | {tr.net_pnl_usd:+.4f} |"
        )

    out_path.write_text("\n".join(sections))


def write_trades_jsonl(trades: list[PairTrade], path: Path) -> None:
    with path.open("w") as f:
        for tr in trades:
            f.write(json.dumps(asdict(tr)) + "\n")


# ---------------------------------------------------------------------------
# Tick enrichment — pull slug into Tick if available
# ---------------------------------------------------------------------------

def enrich_ticks_with_slug(files: list[Path], ticks: list[Tick], window_start: float) -> None:
    """The reused Tick dataclass doesn't carry slug. Stream the same
    files once more to attach a slug per market_id (so the report is
    readable). Cheap because we hash by market_id and stop early."""
    needed = {t.market_id for t in ticks}
    slug_by_market: dict[str, str] = {}
    for fp in files:
        if not needed - slug_by_market.keys():
            break
        with open(fp) as f:
            for line in f:
                if '"daemon_tick"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = e.get("payload") or {}
                mid = str(p.get("market_id") or "")
                if not mid or mid in slug_by_market or mid not in needed:
                    continue
                slug_by_market[mid] = str(p.get("slug") or "")
    # Monkey-patch each Tick with a .slug attribute (the dataclass is
    # frozen-ish but plain Python, so this works for our reporting needs).
    for t in ticks:
        object.__setattr__(t, "slug", slug_by_market.get(t.market_id, ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--pair-cost-max", type=float, default=0.98,
                    help="Sum of (ask_yes − offset) + (ask_no − offset) must be ≤ this to enter. "
                         "0.98 ≈ 2% pre-fee discount.")
    ap.add_argument("--maker-offset", type=float, default=0.01,
                    help="Cents under cheap-side ask to post maker bids on each leg.")
    ap.add_argument("--maker-ttl-seconds", type=float, default=120.0,
                    help="Window to wait for BOTH legs to fill via maker-through.")
    ap.add_argument("--min-tte", type=int, default=300,
                    help="Minimum seconds to expiry at entry. 300s leaves time for fills.")
    ap.add_argument("--max-tte", type=int, default=900,
                    help="Cap to keep us inside the btc_15m family.")
    ap.add_argument("--size-usd", type=float, default=5.0,
                    help="Total dollar size per pair (split across legs).")
    ap.add_argument("--asymmetric", action="store_true",
                    help="Bias size to the cheaper leg (inverse-price weighting, 30/70 clamp).")
    ap.add_argument("--fee-rate", type=float, default=0.02,
                    help="Polymarket profit fee. 0.02 = 2%.")
    ap.add_argument("--no-fetch-resolutions", action="store_true",
                    help="Skip the gamma-api resolution lookup (faster reruns); "
                         "rely on last-tick inference only.")
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--out", type=Path, default=Path("scripts/hedged_pair_backtest.md"))
    ap.add_argument("--trades-out", type=Path, default=Path("scripts/hedged_pair_backtest_trades.jsonl"))
    args = ap.parse_args()

    now = datetime.now(timezone.utc).timestamp()
    window_start = now - args.days * 24 * 3600

    files = [Path(f) for f in args.files] if args.files else detect_files(window_start)
    if not files:
        print("No event-log files found.", file=sys.stderr)
        return 1
    print(f"[files] {len(files)} files", file=sys.stderr)

    ticks = load_ticks(files, window_start, args.max_tte)
    if not ticks:
        print("No ticks in window.", file=sys.stderr)
        return 1

    enrich_ticks_with_slug(files, ticks, window_start)

    signals = find_pair_signals(
        ticks,
        pair_cost_max=args.pair_cost_max,
        maker_offset=args.maker_offset,
        min_tte=args.min_tte,
    )
    print(f"[signals] {len(signals)} pair-eligible markets", file=sys.stderr)

    # Resolve outcomes (cached). Skip the API call when asked.
    cache = load_resolution_cache()
    resolutions: dict[str, tuple[str | None, str]] = {}
    api_hits = 0
    for mticks, i in signals:
        mid = mticks[i].market_id
        if mid in resolutions:
            continue
        if args.no_fetch_resolutions:
            resolutions[mid] = (None, "skipped")
            continue
        out, src = fetch_resolution(mid, cache)
        if src == "api":
            api_hits += 1
        resolutions[mid] = (out, src)
    save_resolution_cache(cache)
    print(f"[resolutions] {api_hits} API fetches, {len(resolutions)} total markets", file=sys.stderr)

    trades: list[PairTrade] = []
    for mticks, i in signals:
        mid = mticks[i].market_id
        out, src = resolutions.get(mid, (None, "missing"))
        trades.append(simulate_pair(
            mticks, i,
            maker_offset=args.maker_offset,
            maker_ttl_seconds=args.maker_ttl_seconds,
            size_usd=args.size_usd,
            asymmetric=args.asymmetric,
            fee_rate=args.fee_rate,
            resolution=out,
            resolution_source=src,
        ))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_report(args, trades, files, args.out)
    write_trades_jsonl(trades, args.trades_out)
    print(f"[out] wrote {args.out} + {args.trades_out}", file=sys.stderr)
    print(file=sys.stderr)
    print(summarize(trades), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
