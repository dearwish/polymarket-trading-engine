"""Mirror-trade simulation for copy-trading.

For each top-ranked curated wallet (from ``copy_wallet_discovery.py``),
fetch ``/activity`` over the recent window, walk BUY → SELL → resolution
in chronological order, and compute the realized P&L of a mirror trader
that copies every BUY with a fixed-USD position cap.

Assumptions (generous to the strategy — the real result would be worse):

- Mirror fills at the SAME price as the source wallet (no latency, no
  slippage from racing other copy bots). Real-world copy-trading
  loses 50-200 bps to this gap on the median trade.
- Mirror SELLs whenever the source wallet sells, at the source's sell
  price. Partial sells matched FIFO against open mirror inventory.
- Positions still open at the end of the window are marked at:
  resolution price ($1 / $0) if market closed, else current YES/NO
  implied probability from gamma-api.
- Polymarket 2% profit fee applied per closed position.
- No per-day position cap, no risk gates, no wallet weighting — pure
  baseline. Realistic deployment would add all of those.

Usage:
  uv run python scripts/copy_mirror_sim.py
  uv run python scripts/copy_mirror_sim.py --days 3 --top-n 20 --mirror-size-usd 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx


ACTIVITY_URL = "https://data-api.polymarket.com/activity"
MARKET_URL = "https://gamma-api.polymarket.com/markets"
WALLET_DISCOVERY_CSV = Path("scripts/copy_wallet_discovery.csv")
ACTIVITY_CACHE_DIR = Path("scripts/_copy_activity_cache")
MARKET_PRICE_CACHE = Path("scripts/_copy_market_price_cache.json")


@dataclass
class MirrorPosition:
    wallet: str
    asset: str
    condition_id: str
    outcome_index: int
    title: str
    event_slug: str
    open_ts: float
    open_price: float
    shares: float          # mirror's shares (size_usd / price)
    cost_usd: float        # mirror's cost (size_usd)
    close_ts: float | None = None
    close_price: float | None = None
    close_reason: str = "open"  # "wallet_sold" / "resolved_win" / "resolved_loss" / "mark_to_market" / "open"
    gross_pnl: float = 0.0
    fee_paid: float = 0.0
    net_pnl: float = 0.0


def load_top_wallets(top_n: int) -> list[tuple[str, str, float]]:
    """Return [(wallet, user_name, score)] for the top_n ranked wallets."""
    if not WALLET_DISCOVERY_CSV.exists():
        print(f"[err] {WALLET_DISCOVERY_CSV} missing. Run copy_wallet_discovery.py first.",
              file=sys.stderr)
        sys.exit(2)
    import csv
    rows: list[tuple[str, str, float]] = []
    with WALLET_DISCOVERY_CSV.open() as f:
        for r in csv.DictReader(f):
            try:
                rows.append((r["wallet"], r["user_name"], float(r["score"] or 0)))
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda t: -t[2])
    return rows[:top_n]


def fetch_activity(
    client: httpx.Client,
    wallet: str,
    *,
    use_cache: bool,
    max_pages: int,
    page_size: int,
    delay_seconds: float,
) -> list[dict]:
    cache_path = ACTIVITY_CACHE_DIR / f"{wallet}.json"
    if use_cache and cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            pass
    out: list[dict] = []
    seen: set[str] = set()
    offset = 0
    for _ in range(max_pages):
        try:
            r = client.get(
                ACTIVITY_URL,
                params={"user": wallet, "limit": page_size, "offset": offset},
                timeout=20,
            )
            r.raise_for_status()
            batch = r.json()
        except Exception as exc:
            print(f"  [warn] {wallet[:10]}… offset={offset} failed: {exc}", file=sys.stderr)
            break
        if not batch:
            break
        added = 0
        for a in batch:
            key = a.get("transactionHash") or f"{a.get('asset')}|{a.get('timestamp')}|{a.get('side')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
            added += 1
        if added == 0:
            break
        offset += page_size
        time.sleep(delay_seconds)
    ACTIVITY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(out))
    return out


def load_market_price_cache() -> dict[str, dict]:
    if MARKET_PRICE_CACHE.exists():
        try:
            return json.loads(MARKET_PRICE_CACHE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_market_price_cache(cache: dict[str, dict]) -> None:
    MARKET_PRICE_CACHE.write_text(json.dumps(cache, indent=2))


def fetch_market_state(
    client: httpx.Client,
    condition_id: str,
    cache: dict[str, dict],
) -> dict | None:
    """Return {'closed': bool, 'yes_price': float|None}.

    yes_price is the resolution price if closed, else current implied.
    """
    if condition_id in cache:
        return cache[condition_id]
    # Default /markets endpoint filters out closed markets — many of our open
    # mirror positions are on already-resolved markets. Try active first, then
    # fall back to closed=true if the active query is empty.
    data: list | None = None
    for params in (
        {"condition_ids": condition_id},
        {"condition_ids": condition_id, "closed": "true"},
    ):
        try:
            r = client.get(MARKET_URL, params=params, timeout=15)
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            print(f"  [warn] market {condition_id[:10]}… failed: {exc}", file=sys.stderr)
            return None
        if isinstance(payload, list) and payload:
            data = payload
            break
    if not data:
        cache[condition_id] = {"closed": False, "yes_price": None}
        return cache[condition_id]
    m = data[0]
    closed = bool(m.get("closed") or m.get("isResolved"))
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = None
    yes_price = None
    if isinstance(prices, list) and len(prices) >= 1:
        try:
            yes_price = float(prices[0])
        except (TypeError, ValueError):
            yes_price = None
    cache[condition_id] = {"closed": closed, "yes_price": yes_price}
    return cache[condition_id]


def simulate_wallet(
    wallet: str,
    user_name: str,
    activity: list[dict],
    window_start_ts: float,
    window_end_ts: float,
    mirror_size_usd: float,
    price_cache: dict[str, dict],
    client: httpx.Client,
    fee_rate: float,
) -> list[MirrorPosition]:
    """Walk activity chronologically and simulate the mirror's book."""
    # Sort old → new by timestamp.
    acts = sorted(
        [a for a in activity if str(a.get("type")) == "TRADE"],
        key=lambda a: float(a.get("timestamp") or 0),
    )
    # FIFO inventory per asset.
    open_by_asset: dict[str, list[MirrorPosition]] = defaultdict(list)
    closed: list[MirrorPosition] = []

    for a in acts:
        ts = float(a.get("timestamp") or 0)
        if ts > window_end_ts:
            break
        side = str(a.get("side") or "").upper()
        price = float(a.get("price") or 0)
        if price <= 0 or price >= 1:
            continue
        asset = str(a.get("asset") or "")
        if not asset:
            continue

        if side == "BUY":
            if ts < window_start_ts:
                # Pre-window BUY — irrelevant unless followed by an in-window SELL,
                # which we'd then have nothing to close. Skip cleanly.
                continue
            shares = mirror_size_usd / price
            pos = MirrorPosition(
                wallet=wallet,
                asset=asset,
                condition_id=str(a.get("conditionId") or ""),
                outcome_index=int(a.get("outcomeIndex") or 0),
                title=str(a.get("title") or ""),
                event_slug=str(a.get("eventSlug") or ""),
                open_ts=ts,
                open_price=price,
                shares=shares,
                cost_usd=mirror_size_usd,
            )
            open_by_asset[asset].append(pos)
        elif side == "SELL":
            # FIFO-match against any open mirror positions on the same asset.
            queue = open_by_asset.get(asset) or []
            sell_shares_remaining = float(a.get("size") or 0)
            # Mirror tracks shares per its own size, not the wallet's, so we
            # interpret the wallet's SELL as "close some fraction of mirror
            # holdings." If the wallet sells X% of its inventory, the mirror
            # closes the same X% — implemented here as FIFO over mirror lots
            # weighted by the wallet's sell fraction.
            #
            # Simpler model used: each wallet SELL fully closes the oldest open
            # mirror lot (one-to-one with the wallet's trades). This matches the
            # rough scale of mirror-bot behavior in practice.
            if queue:
                lot = queue.pop(0)
                lot.close_ts = ts
                lot.close_price = price
                lot.close_reason = "wallet_sold"
                gross = (price - lot.open_price) * lot.shares
                fee = max(0.0, gross) * fee_rate
                lot.gross_pnl = gross
                lot.fee_paid = fee
                lot.net_pnl = gross - fee
                closed.append(lot)

    # Mark remaining open positions to current state.
    for asset, lots in open_by_asset.items():
        for lot in lots:
            state = fetch_market_state(client, lot.condition_id, price_cache) if lot.condition_id else None
            if state is None or state.get("yes_price") is None:
                # Couldn't fetch — mark unresolved at open_price (zero P&L).
                lot.close_ts = window_end_ts
                lot.close_price = lot.open_price
                lot.close_reason = "unresolved_unknown"
                lot.gross_pnl = 0.0
                lot.net_pnl = 0.0
                closed.append(lot)
                continue
            yes_price = float(state["yes_price"])
            # Token side: outcomeIndex 0 = YES, 1 = NO.
            mirror_side_price = yes_price if lot.outcome_index == 0 else (1 - yes_price)
            mirror_side_price = max(0.0, min(1.0, mirror_side_price))
            if state.get("closed"):
                # Resolved.
                lot.close_reason = "resolved_win" if mirror_side_price >= 0.95 else (
                    "resolved_loss" if mirror_side_price <= 0.05 else "resolved_ambiguous"
                )
            else:
                lot.close_reason = "mark_to_market"
            lot.close_ts = window_end_ts
            lot.close_price = mirror_side_price
            gross = (mirror_side_price - lot.open_price) * lot.shares
            fee = max(0.0, gross) * fee_rate
            lot.gross_pnl = gross
            lot.fee_paid = fee
            lot.net_pnl = gross - fee
            closed.append(lot)

    return closed


def summarize(positions: list[MirrorPosition], label: str) -> str:
    if not positions:
        return f"### {label}\n\n_No mirror trades in window._\n"
    n = len(positions)
    by_reason: dict[str, list[MirrorPosition]] = defaultdict(list)
    for p in positions:
        by_reason[p.close_reason].append(p)
    wins = [p for p in positions if p.net_pnl > 0]
    losses = [p for p in positions if p.net_pnl < 0]
    total_pnl = sum(p.net_pnl for p in positions)
    total_cost = sum(p.cost_usd for p in positions)
    total_gross = sum(p.gross_pnl for p in positions)
    total_fee = sum(p.fee_paid for p in positions)
    roi = (total_pnl / total_cost) if total_cost > 0 else 0
    out = [f"### {label}\n"]
    out.append("| Metric | Value |")
    out.append("|---|---:|")
    out.append(f"| mirror trades | {n} |")
    out.append(f"| total $ deployed | ${total_cost:,.2f} |")
    out.append(f"| wins | {len(wins)} |")
    out.append(f"| losses | {len(losses)} |")
    out.append(f"| win rate | {len(wins)/max(n,1):.1%} |")
    out.append(f"| total gross P&L ($) | {total_gross:+,.2f} |")
    out.append(f"| total fees ($) | {total_fee:.2f} |")
    out.append(f"| **total net P&L ($)** | **{total_pnl:+,.2f}** |")
    out.append(f"| **ROI on capital deployed** | **{roi:+.1%}** |")
    out.append("")
    out.append("**By close reason**")
    out.append("")
    out.append("| reason | n | net PnL | avg net |")
    out.append("|---|---:|---:|---:|")
    for reason, lots in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        total = sum(p.net_pnl for p in lots)
        out.append(f"| `{reason}` | {len(lots)} | {total:+.2f} | {total/len(lots):+.3f} |")
    return "\n".join(out) + "\n"


def write_report(
    args,
    per_wallet: list[tuple[str, str, list[MirrorPosition]]],
    out_path: Path,
) -> None:
    sections: list[str] = []
    sections.append("# Copy-Trading Mirror Simulation\n")
    sections.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}\n")
    sections.append("## Configuration\n")
    sections.append("| Setting | Value |")
    sections.append("|---|---|")
    sections.append(f"| `--days` | {args.days} |")
    sections.append(f"| `--top-n` (wallets curated) | {args.top_n} |")
    sections.append(f"| `--mirror-size-usd` | ${args.mirror_size_usd} per BUY |")
    sections.append(f"| `--fee-rate` | {args.fee_rate} |")
    sections.append("")
    sections.append("## Assumptions (generous)\n")
    sections.append("- Mirror fills at the **source wallet's exact price** (no latency, no slippage)")
    sections.append("- Each wallet SELL closes ONE mirror lot FIFO at the source's sell price")
    sections.append("- Open-at-window-end positions marked at resolution ($1/$0) or current implied")
    sections.append("- No copy-bot competition modeled; real-world fills would be materially worse\n")

    all_positions: list[MirrorPosition] = []
    for w, n, lots in per_wallet:
        all_positions.extend(lots)

    sections.append("## Portfolio total (all wallets)\n")
    sections.append(summarize(all_positions, "All curated wallets combined"))

    sections.append("## Per-wallet breakdown\n")
    sections.append("| wallet | user_name | trades | $ deployed | wins | net P&L | ROI |")
    sections.append("|---|---|---:|---:|---:|---:|---:|")
    rows = []
    for w, n, lots in per_wallet:
        if not lots:
            rows.append((w, n, 0, 0.0, 0, 0.0, 0.0))
            continue
        cost = sum(l.cost_usd for l in lots)
        pnl = sum(l.net_pnl for l in lots)
        wins = sum(1 for l in lots if l.net_pnl > 0)
        roi = pnl / cost if cost > 0 else 0
        rows.append((w, n, len(lots), cost, wins, pnl, roi))
    rows.sort(key=lambda r: -r[5])
    for w, n, count, cost, wins, pnl, roi in rows:
        short = w[:6] + "…" + w[-4:]
        sections.append(f"| `{short}` | {n[:18]} | {count} | ${cost:,.0f} | {wins} | {pnl:+,.2f} | {roi:+.1%} |")
    out_path.write_text("\n".join(sections))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--top-n", type=int, default=20,
                    help="Mirror trades from this many top-scored curated wallets.")
    ap.add_argument("--mirror-size-usd", type=float, default=5.0,
                    help="Mirror's per-BUY dollar size.")
    ap.add_argument("--fee-rate", type=float, default=0.02)
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--delay-seconds", type=float, default=0.2)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("scripts/copy_mirror_sim.md"))
    args = ap.parse_args()

    now = datetime.now(timezone.utc).timestamp()
    window_start = now - args.days * 86400
    window_end = now

    wallets = load_top_wallets(args.top_n)
    print(f"[wallets] mirroring top {len(wallets)} by composite score", file=sys.stderr)

    price_cache = load_market_price_cache()
    per_wallet: list[tuple[str, str, list[MirrorPosition]]] = []

    with httpx.Client() as client:
        for idx, (wallet, user_name, score) in enumerate(wallets, 1):
            print(f"[wallet {idx}/{len(wallets)}] {wallet[:10]}… ({user_name[:18]}, score={score:.2f})",
                  file=sys.stderr)
            activity = fetch_activity(
                client, wallet,
                use_cache=not args.no_cache,
                max_pages=args.max_pages,
                page_size=args.page_size,
                delay_seconds=args.delay_seconds,
            )
            # Filter pre-screen to in-window or with in-window exits.
            in_window = [a for a in activity
                         if window_start <= float(a.get("timestamp") or 0) <= window_end
                         and str(a.get("type")) == "TRADE"]
            print(f"  → {len(activity)} actions total, {len(in_window)} in window", file=sys.stderr)
            positions = simulate_wallet(
                wallet, user_name, activity,
                window_start_ts=window_start,
                window_end_ts=window_end,
                mirror_size_usd=args.mirror_size_usd,
                price_cache=price_cache,
                client=client,
                fee_rate=args.fee_rate,
            )
            per_wallet.append((wallet, user_name, positions))

    save_market_price_cache(price_cache)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_report(args, per_wallet, args.out)
    print(f"[out] wrote {args.out}", file=sys.stderr)

    # Print headline result.
    all_pos = [p for _, _, lots in per_wallet for p in lots]
    if all_pos:
        total_pnl = sum(p.net_pnl for p in all_pos)
        total_cost = sum(p.cost_usd for p in all_pos)
        print(f"\n[SUMMARY] {len(all_pos)} mirror trades, ${total_cost:,.0f} deployed, "
              f"net P&L ${total_pnl:+,.2f} ({total_pnl/max(total_cost,1):.1%} ROI)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
