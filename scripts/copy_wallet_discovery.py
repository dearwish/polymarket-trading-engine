"""Wallet-curation discovery for the copy-trading direction (Phase 4).

Reads:
  - Polymarket leaderboard (data-api.polymarket.com/v1/leaderboard)
    — top-N wallets by all-time realized PnL (it's noisy, see below).
  - Polymarket closed-positions per wallet
    (data-api.polymarket.com/closed-positions?user=<wallet>) — per-trade
    realized PnL, average price, slug, timestamp, outcome.

Writes:
  - scripts/copy_wallet_discovery_report.md
  - scripts/copy_wallet_discovery.csv
  - scripts/_copy_wallet_cache/<wallet>.json — per-wallet closed-positions
    cache so reruns are fast.

Goal: decide whether copy-trading has any alpha worth building before
investing in a Polygon RPC sidecar. The plan's kill-criterion is set
inside the report itself: the curated top-20 wallets' distribution of
6-month-equivalent edge tells us whether mirroring is signal or noise.

Per-wallet metrics:
  - history_days: span between first and last closed trade
  - n_trades: closed-position count
  - total_realized_pnl_usd
  - total_volume_usd
  - pnl_per_dollar_vol: PnL efficiency (return-on-volume)
  - hit_rate: fraction of trades with realizedPnl > 0
  - top_trade_share: largest single trade's share of total PnL
    (penalizes "one lucky bet" wallets)
  - mean / median trade PnL
  - category split: count of trades by event-slug prefix
    (epl-/cbb-/nfl-/btc-updown-/pres-/etc.)

Output ranks wallets by a composite "tradability" score that rewards
history length + trade count + diversification and penalizes top-trade
concentration. The user reviews the ranked list and decides if any
wallets are worth allocating real capital to mirror.

Usage:
  uv run python scripts/copy_wallet_discovery.py
  uv run python scripts/copy_wallet_discovery.py --top-n 100 --max-pages 5
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
CLOSED_POSITIONS_URL = "https://data-api.polymarket.com/closed-positions"
CACHE_DIR = Path("scripts/_copy_wallet_cache")


# ---------------------------------------------------------------------------
# Fetchers (with on-disk cache)
# ---------------------------------------------------------------------------

def fetch_leaderboard(client: httpx.Client, top_n: int) -> list[dict]:
    """The leaderboard endpoint ignores period params — it returns
    all-time PnL ranking. We pull top_n and filter downstream."""
    r = client.get(LEADERBOARD_URL, params={"limit": top_n}, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_wallet_closed_positions(
    client: httpx.Client,
    wallet: str,
    *,
    use_cache: bool,
    max_pages: int,
    page_size: int,
    delay_seconds: float,
) -> list[dict]:
    """Fetch all closed positions for a wallet, page through and dedupe."""
    cache_path = CACHE_DIR / f"{wallet}.json"
    if use_cache and cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            pass

    positions: list[dict] = []
    seen_assets: set[str] = set()
    offset = 0
    for _ in range(max_pages):
        try:
            r = client.get(
                CLOSED_POSITIONS_URL,
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
        added_this_page = 0
        for p in batch:
            key = f"{p.get('asset')}|{p.get('timestamp')}"
            if key in seen_assets:
                continue
            seen_assets.add(key)
            positions.append(p)
            added_this_page += 1
        if added_this_page == 0:
            # API returned overlapping page — stop.
            break
        offset += page_size
        time.sleep(delay_seconds)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(positions))
    return positions


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

CATEGORY_PREFIXES = [
    ("crypto_candle", re.compile(r"^(btc|eth|sol|xrp|doge)-updown-")),
    ("crypto_threshold", re.compile(r"^(bitcoin|ethereum|solana)-(up-or-down|above|below)-")),
    ("epl", re.compile(r"^epl-")),
    ("nba", re.compile(r"^nba-")),
    ("nfl", re.compile(r"^nfl-")),
    ("nhl", re.compile(r"^nhl-")),
    ("mlb", re.compile(r"^mlb-")),
    ("ncaa", re.compile(r"^(cbb|cfb|ncaa)-")),
    ("ufc", re.compile(r"^ufc-")),
    ("tennis", re.compile(r"^(atp|wta|tennis)-")),
    ("soccer", re.compile(r"^(uefa|fifa|champions|laliga|seriea|bundesliga|ligue1|mls)-")),
    ("politics_us", re.compile(r"^(presidential|us-|senate|house|trump|biden|harris|election)-")),
    ("politics_intl", re.compile(r"^(uk-|germany|france|israel|russia|ukraine|china|nato)-")),
    ("crypto_other", re.compile(r"^(bitcoin|ethereum|solana|crypto)-")),
    ("entertainment", re.compile(r"^(oscar|emmy|grammy|spotify|netflix|tv-|movie-)-")),
    ("economy", re.compile(r"^(fed|cpi|gdp|rates|unemployment|stocks)-")),
]


def classify_slug(slug: str) -> str:
    s = (slug or "").lower()
    for name, regex in CATEGORY_PREFIXES:
        if regex.match(s):
            return name
    return "other"


@dataclass
class WalletStats:
    wallet: str
    user_name: str
    lb_rank: int | None
    lb_pnl: float
    lb_vol: float
    # Per-wallet stats from closed-positions
    n_trades: int = 0
    total_realized_pnl_usd: float = 0.0
    total_volume_usd: float = 0.0
    pnl_per_dollar_vol: float = 0.0
    hit_rate: float = 0.0
    mean_trade_pnl: float = 0.0
    median_trade_pnl: float = 0.0
    top_trade_pnl: float = 0.0
    top_trade_share: float = 0.0  # |top|/sum|pnl|
    history_first_ts: float = 0.0
    history_last_ts: float = 0.0
    history_days: float = 0.0
    avg_position_size_usd: float = 0.0
    category_counts: dict[str, int] = field(default_factory=dict)
    dominant_category: str = ""
    dominant_category_share: float = 0.0
    score: float = 0.0  # composite tradability score
    score_reasons: list[str] = field(default_factory=list)


def compute_stats(
    wallet: str,
    user_name: str,
    lb_rank: int | None,
    lb_pnl: float,
    lb_vol: float,
    positions: list[dict],
) -> WalletStats:
    ws = WalletStats(
        wallet=wallet,
        user_name=user_name,
        lb_rank=lb_rank,
        lb_pnl=lb_pnl,
        lb_vol=lb_vol,
    )
    if not positions:
        return ws

    pnls = [float(p.get("realizedPnl") or 0) for p in positions]
    volumes = [float(p.get("totalBought") or 0) for p in positions]
    timestamps = [float(p.get("timestamp") or 0) for p in positions if p.get("timestamp")]

    ws.n_trades = len(positions)
    ws.total_realized_pnl_usd = sum(pnls)
    ws.total_volume_usd = sum(volumes)
    ws.pnl_per_dollar_vol = (
        ws.total_realized_pnl_usd / ws.total_volume_usd if ws.total_volume_usd > 0 else 0.0
    )
    ws.hit_rate = sum(1 for p in pnls if p > 0) / max(len(pnls), 1)
    ws.mean_trade_pnl = ws.total_realized_pnl_usd / max(len(pnls), 1)
    ws.median_trade_pnl = statistics.median(pnls) if pnls else 0.0
    abs_pnls = [abs(p) for p in pnls]
    top_abs = max(abs_pnls) if abs_pnls else 0.0
    sum_abs = sum(abs_pnls) or 1.0
    ws.top_trade_pnl = max(pnls) if pnls else 0.0
    ws.top_trade_share = top_abs / sum_abs
    ws.avg_position_size_usd = ws.total_volume_usd / max(len(positions), 1)

    if timestamps:
        ws.history_first_ts = min(timestamps)
        ws.history_last_ts = max(timestamps)
        ws.history_days = (ws.history_last_ts - ws.history_first_ts) / 86400

    cats: dict[str, int] = defaultdict(int)
    for p in positions:
        slug = str(p.get("eventSlug") or p.get("slug") or "")
        cats[classify_slug(slug)] += 1
    ws.category_counts = dict(cats)
    if cats:
        dom_cat, dom_n = max(cats.items(), key=lambda kv: kv[1])
        ws.dominant_category = dom_cat
        ws.dominant_category_share = dom_n / len(positions)

    # Composite tradability score. Higher = more attractive to mirror.
    # Each factor is normalized to roughly 0..1 with caps so a single
    # blow-out feature can't dominate.
    score = 0.0
    reasons: list[str] = []

    # 1. History length: full credit at 6 months (180d), zero below 30d.
    hist_score = max(0.0, min(1.0, (ws.history_days - 30) / 150))
    score += 2.0 * hist_score
    if hist_score < 0.5:
        reasons.append(f"history<5mo ({ws.history_days:.0f}d)")

    # 2. Trade count: full credit at 200, zero below 20.
    nt_score = max(0.0, min(1.0, (ws.n_trades - 20) / 180))
    score += 2.0 * nt_score
    if nt_score < 0.5:
        reasons.append(f"few trades ({ws.n_trades})")

    # 3. Top-trade share: penalize >30% concentration heavily.
    conc_score = max(0.0, 1.0 - max(0.0, ws.top_trade_share - 0.10) / 0.40)
    score += 2.0 * conc_score
    if ws.top_trade_share > 0.30:
        reasons.append(f"top-trade {ws.top_trade_share:.0%} of |PnL|")

    # 4. Hit rate: reward >55%, penalize <50%.
    hr_score = max(0.0, min(1.0, (ws.hit_rate - 0.45) / 0.20))
    score += 1.5 * hr_score
    if ws.hit_rate < 0.50:
        reasons.append(f"hit rate {ws.hit_rate:.0%}")

    # 5. Return-on-volume: reward >5%.
    rov_score = max(0.0, min(1.0, ws.pnl_per_dollar_vol / 0.10))
    score += 1.5 * rov_score

    # 6. Diversification: penalize >70% in one category (might be a
    # single-domain specialist, but for copy-trading that's narrow).
    div_score = max(0.0, 1.0 - max(0.0, ws.dominant_category_share - 0.50) / 0.40)
    score += 1.0 * div_score
    if ws.dominant_category_share > 0.70:
        reasons.append(f"{ws.dominant_category} {ws.dominant_category_share:.0%}")

    ws.score = score
    ws.score_reasons = reasons
    return ws


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_csv(stats: list[WalletStats], path: Path) -> None:
    fieldnames = [
        "rank_by_score", "score", "wallet", "user_name", "lb_rank", "lb_pnl", "lb_vol",
        "n_trades", "history_days", "total_realized_pnl_usd", "total_volume_usd",
        "pnl_per_dollar_vol", "hit_rate", "mean_trade_pnl", "median_trade_pnl",
        "top_trade_pnl", "top_trade_share", "avg_position_size_usd",
        "dominant_category", "dominant_category_share", "score_reasons",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for i, s in enumerate(stats, 1):
            row = asdict(s)
            row["rank_by_score"] = i
            row["score_reasons"] = "; ".join(s.score_reasons)
            row.pop("category_counts", None)
            w.writerow(row)


def write_report(stats: list[WalletStats], top_k: int, path: Path) -> None:
    sections: list[str] = []
    sections.append("# Copy-Trading Wallet Discovery\n")
    sections.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}\n")
    sections.append(f"**Wallets analyzed:** {len(stats)}\n")
    sections.append("")
    sections.append("## What this report tests\n")
    sections.append(
        "Whether the Polymarket leaderboard's top wallets show evidence of *repeatable* "
        "edge that would justify building a copy-trading sidecar. Per the plan's Phase 4 "
        "scope: if the top-20 by composite score don't look like sustained, diversified, "
        "non-lottery-ticket alpha, the copy-trading direction is **not pursued**.\n"
    )
    sections.append("## Composite score (per-wallet)\n")
    sections.append("Range ~0–10. Components (each with caps so no single factor dominates):\n")
    sections.append("- **History length** (×2): full credit at ≥180 days, zero below 30")
    sections.append("- **Trade count** (×2): full credit at ≥200, zero below 20")
    sections.append("- **Top-trade-share** (×2): penalty when one trade is >30% of |PnL|")
    sections.append("- **Hit rate** (×1.5): reward >55%, penalty <50%")
    sections.append("- **PnL per $ volume** (×1.5): reward >5% return-on-volume")
    sections.append("- **Diversification** (×1): penalty when >70% of trades are in one category")
    sections.append("")
    sections.append(f"## Top {top_k} wallets by composite score\n")
    sections.append(
        "| # | wallet | name | score | hist_d | trades | hit | $vol | pnl | pnl/vol | top% | dom_cat | notes |"
    )
    sections.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for i, s in enumerate(stats[:top_k], 1):
        short_w = s.wallet[:6] + "…" + s.wallet[-4:]
        notes = "; ".join(s.score_reasons[:3])
        sections.append(
            f"| {i} | `{short_w}` | {s.user_name[:18]} | {s.score:.2f} | {s.history_days:.0f} | "
            f"{s.n_trades} | {s.hit_rate:.0%} | ${s.total_volume_usd:,.0f} | "
            f"${s.total_realized_pnl_usd:+,.0f} | {s.pnl_per_dollar_vol:+.1%} | "
            f"{s.top_trade_share:.0%} | {s.dominant_category} | {notes} |"
        )
    sections.append("")

    # Distribution stats so the kill-criterion is grounded in data.
    if stats:
        scores = [s.score for s in stats]
        top20 = stats[:20]
        top20_scores = [s.score for s in top20]
        top20_history = [s.history_days for s in top20]
        top20_pnl_per_vol = [s.pnl_per_dollar_vol for s in top20 if s.total_volume_usd > 0]
        top20_topshare = [s.top_trade_share for s in top20]
        sections.append("## Kill-criterion check (top-20)\n")
        sections.append("| Metric | Median | Mean | Min | Max |")
        sections.append("|---|---:|---:|---:|---:|")
        sections.append(
            f"| composite score | {statistics.median(top20_scores):.2f} | "
            f"{statistics.mean(top20_scores):.2f} | {min(top20_scores):.2f} | {max(top20_scores):.2f} |"
        )
        if top20_history:
            sections.append(
                f"| history (days) | {statistics.median(top20_history):.0f} | "
                f"{statistics.mean(top20_history):.0f} | {min(top20_history):.0f} | {max(top20_history):.0f} |"
            )
        if top20_pnl_per_vol:
            sections.append(
                f"| pnl/$vol | {statistics.median(top20_pnl_per_vol):.2%} | "
                f"{statistics.mean(top20_pnl_per_vol):.2%} | "
                f"{min(top20_pnl_per_vol):.2%} | {max(top20_pnl_per_vol):.2%} |"
            )
        sections.append(
            f"| top-trade share | {statistics.median(top20_topshare):.0%} | "
            f"{statistics.mean(top20_topshare):.0%} | {min(top20_topshare):.0%} | {max(top20_topshare):.0%} |"
        )
        sections.append("")
        sections.append("### Decision rule\n")
        sections.append(
            "Build the copy-trading sidecar **only if** the top-20 by score show ALL of:\n"
            "- median history ≥ 120 days,\n"
            "- median trades ≥ 100,\n"
            "- median PnL per $ volume ≥ 5%,\n"
            "- median top-trade share ≤ 30%.\n"
            "\n"
            "If any of those four fails, the leaderboard signal is dominated by lucky "
            "short-run bets or extreme concentration, and mirroring will not survive "
            "out-of-sample. Stop. Re-evaluate after collecting better wallet curation "
            "data (e.g. Polymarket subgraph by category, manual annotation).\n"
        )
        # Auto-evaluate the rule
        med_hist = statistics.median(top20_history) if top20_history else 0
        med_trades = statistics.median([s.n_trades for s in top20])
        med_pnlvol = statistics.median(top20_pnl_per_vol) if top20_pnl_per_vol else 0
        med_topshare = statistics.median(top20_topshare) if top20_topshare else 1.0
        passes = [
            (med_hist >= 120, f"history ≥120d (got {med_hist:.0f}d)"),
            (med_trades >= 100, f"trades ≥100 (got {med_trades:.0f})"),
            (med_pnlvol >= 0.05, f"pnl/vol ≥5% (got {med_pnlvol:.1%})"),
            (med_topshare <= 0.30, f"top-trade share ≤30% (got {med_topshare:.0%})"),
        ]
        sections.append("### Verdict\n")
        passing = [p[1] for p in passes if p[0]]
        failing = [p[1] for p in passes if not p[0]]
        sections.append(f"- **passes** ({len(passing)}/4): " + (", ".join(passing) or "—"))
        sections.append(f"- **fails** ({len(failing)}/4): " + (", ".join(failing) or "—"))
        if failing:
            sections.append("\n**Outcome:** Do not build the copy-trading sidecar. "
                            "The leaderboard signal does not survive the kill-criterion.\n")
        else:
            sections.append("\n**Outcome:** Top-20 wallets cleared the kill-criterion. "
                            "Worth a follow-up plan to design the Polygon RPC sidecar that "
                            "mirrors the curated wallet allowlist.\n")

    path.write_text("\n".join(sections))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=100,
                    help="Number of leaderboard wallets to analyze.")
    ap.add_argument("--max-pages", type=int, default=10,
                    help="Closed-positions pagination cap per wallet.")
    ap.add_argument("--page-size", type=int, default=100,
                    help="Closed-positions page size.")
    ap.add_argument("--delay-seconds", type=float, default=0.25,
                    help="Politeness delay between API calls.")
    ap.add_argument("--no-cache", action="store_true",
                    help="Force re-fetch of all wallets (default uses on-disk cache).")
    ap.add_argument("--top-k", type=int, default=30,
                    help="How many top-ranked wallets to print in the report table.")
    ap.add_argument("--report", type=Path,
                    default=Path("scripts/copy_wallet_discovery_report.md"))
    ap.add_argument("--csv", type=Path, default=Path("scripts/copy_wallet_discovery.csv"))
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client:
        print(f"[lb] fetching top {args.top_n} from leaderboard…", file=sys.stderr)
        lb = fetch_leaderboard(client, args.top_n)
        print(f"[lb] got {len(lb)} wallets", file=sys.stderr)

        stats: list[WalletStats] = []
        for idx, entry in enumerate(lb, 1):
            wallet = (entry.get("proxyWallet") or "").lower()
            if not wallet:
                continue
            user_name = entry.get("userName") or ""
            lb_pnl = float(entry.get("pnl") or 0)
            lb_vol = float(entry.get("vol") or 0)
            try:
                lb_rank = int(entry.get("rank") or idx)
            except (TypeError, ValueError):
                lb_rank = idx

            print(f"[wallet {idx}/{len(lb)}] {wallet[:10]}… ({user_name[:20]})", file=sys.stderr)
            positions = fetch_wallet_closed_positions(
                client, wallet,
                use_cache=not args.no_cache,
                max_pages=args.max_pages,
                page_size=args.page_size,
                delay_seconds=args.delay_seconds,
            )
            ws = compute_stats(wallet, user_name, lb_rank, lb_pnl, lb_vol, positions)
            stats.append(ws)

    # Rank by composite score descending.
    stats.sort(key=lambda s: -s.score)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_csv(stats, args.csv)
    write_report(stats, args.top_k, args.report)
    print(f"[out] wrote {args.report} + {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
