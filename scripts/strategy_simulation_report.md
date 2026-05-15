# Strategy Simulation Report — Last 3 Days

**Generated:** 2026-05-16 (overnight run)
**Window:** last 3 days of real Polymarket data (engine soak ticks + live API)
**Strategies tested:** hedged-pair (gabagool), copy-trading mirror (top-20 curated wallets)

## TL;DR

| Strategy | Trades | $ Deployed | Net P&L | ROI | Verdict |
|---|---:|---:|---:|---:|---|
| **Hedged-pair (gabagool)** | 91 | $455 | **−$43.17** | **−9.5%** | Don't build (confirms 14d backtest) |
| **Copy mirror — top 20 indiscriminate** | 19,289 | $96,445 | **+$4,099** | **+4.3%** | Headline-positive but qualified |
| Copy mirror — realized-only (excl. mark-to-market) | 12,641 | ~$63K | +$900 | +1.4% | Real-money edge is razor-thin |
| Copy mirror — top-12 winners-in-window only | ~12,000 | ~$60K | **+$13,113** | **+21.8%** | Promising — but selection is in-sample |

**Live strategy reference (from soak-analysis 24h):**
- `fade`: 37 closes, win rate 16%, **−$11.37**
- `adaptive_v2`: 6 closes, win rate 17%, **−$1.32**

Both production strategies are losing money in the current window. The copy-trading mirror is the only signal that looked positive in this simulation — but with caveats below.

## Hedged-pair (gabagool) — confirms the kill

Re-ran the existing backtest ([`scripts/hedged_pair_backtest.py`](hedged_pair_backtest.py)) over 3 days at the loosest configurable threshold:

```
--days 3 --pair-cost-max 0.99 --maker-offset 0.01
```

| Metric | Value |
|---|---:|
| pair-eligible signals | 91 |
| both legs filled (pair locked) | 44 (48.4%) |
| paired-trade win rate | 45.5% |
| single-leg flatten failures | 39 |
| mean P&L per paired trade | +$0.016 |
| **total net P&L** | **−$43.17** |

Same shape as the 14-day run: single-leg failures (39 of 91 signals) crush a near-zero paired-trade edge. The article's claim is consistently unprofitable on this engine's real soak data. **Verdict unchanged: do not build Phase 2.**

## Copy-trading mirror simulation — new this session

Built [`scripts/copy_mirror_sim.py`](copy_mirror_sim.py) — pulls `/activity` from data-api.polymarket.com for each curated wallet, walks BUY → SELL → resolution chronologically, mirrors each BUY at the same price with a $5 cap. Open positions at end of window are marked at resolution price ($1/$0) when closed, or current implied probability when still active.

**Critical assumptions (generous):**
- Mirror fills at the source wallet's EXACT price — no latency, no slippage from racing other copy bots. Real-world mirror execution would lose 50–200 bps to this gap.
- Each wallet SELL closes one mirror lot FIFO. Doesn't model partial sells or position-sizing rules.
- No copy-bot competition modeled.

**Patched mid-run**: gamma-api `/markets?condition_ids=...` returns `[]` for already-resolved markets by default. Added `closed=true` fallback to recover them — without the fix, 63% of mirror positions fell into `unresolved_unknown` and were silently bookmarked at $0 P&L.

### Portfolio totals (post-fix)

| Metric | Value |
|---|---:|
| mirror trades | 19,289 |
| $ deployed | $96,445 |
| wins | 9,562 |
| losses | 9,679 |
| win rate | 49.6% |
| total gross P&L | **+$5,015** |
| total fees (2% on profit) | $916 |
| **total net P&L** | **+$4,099** |
| **ROI on capital** | **+4.3%** |

### Breakdown by close reason (where the P&L actually came from)

| reason | n | net P&L | avg per trade |
|---|---:|---:|---:|
| `resolved_win` (market closed, our side won) | 5,987 | **+$30,364** | +$5.07 |
| `resolved_loss` (market closed, our side lost) | 6,035 | **−$30,175** | −$5.00 |
| `mark_to_market` (still open, marked at current implied) | 6,648 | +$3,199 | +$0.48 |
| `wallet_sold` (closed because source wallet sold) | 619 | +$711 | +$1.15 |

**Realized-only edge (resolved + wallet_sold): +$900 across 12,641 closed trades on ~$63K deployed = +1.4% ROI.**

The headline +$4,099 is inflated by ~$3,200 of paper mark-to-market gains on positions that haven't resolved yet. If those positions move adversely between now and resolution, the realized number could compress further — possibly to breakeven or worse.

### Per-wallet dispersion (the most important finding)

The aggregate hides wild per-wallet dispersion. Composite score does NOT predict mirror-worthiness.

**Winners (12 wallets, sum +$13,113):**

| wallet | user_name | trades | net P&L | ROI |
|---|---|---:|---:|---:|
| `0x2005…75ea` | RN1 | 1858 | +$3,572 | +38.5% |
| `0x21ec…5348` | (anon) | 932 | +$2,787 | +59.8% |
| `0x1117…3532` | benwyatt | 1962 | +$1,847 | +18.8% |
| `0x5aa9…febe` | apucimama | 777 | +$1,366 | +35.2% |
| `0xf68a…5b1b` | iDARKenjoyer | 1218 | +$1,340 | +22.0% |
| ...7 more smaller winners | | | +$2,201 | |

**Losers (8 wallets, sum −$8,815):**

| wallet | user_name | trades | net P&L | ROI |
|---|---|---:|---:|---:|
| `0x1341…0853` | NRASCHATTER | 1854 | **−$3,481** | −37.5% |
| `0xa5ea…d96a` | bossoskil1 | 1981 | **−$2,203** | −22.2% |
| `0x5e6e…be00` | lebronstan23 | 587 | −$1,415 | −48.2% |
| `0xa8e0…ad50` | Wannac | 1005 | −$856 | −17.0% |
| ...4 more smaller losers | | | −$861 | |

**Composite score is not predictive.** Top 10 by score includes both the biggest winner (RN1, score 10.00) and the biggest loser (NRASCHATTER, score 9.53). The dispersion across wallets is wider than the aggregate edge — meaning blind mirror-all-top-20 is a coin-flip allocation across personalities.

### What this changes about the Phase 4 verdict

The earlier [`copy_wallet_discovery_verdict.md`](copy_wallet_discovery_verdict.md) said "auto-criteria pass 4/4 but criteria are too weak, don't build yet." This simulation confirms that:

1. **The aggregate headline (+4.3% ROI) is real but driven by 4-5 wallets.** Indiscriminate copying of "top-N by composite score" loses to indiscriminate strategy noise.
2. **Realized-only edge is only +1.4%** — barely above the costs of running a Polygon RPC sidecar.
3. **The mark-to-market component (~$3,200) is unhedged exposure.** Some of that paper P&L will turn into realized losses at resolution.
4. **The wallet-selection problem is the actual problem.** The leaderboard-by-PnL pool contains both real signal and big losers; copy-trading without out-of-sample wallet filtering is essentially noise trading at scale.

The right shape for copy-trading IF it's built:

- **Per-wallet cohort persistence**: track each candidate wallet's *forward* PnL over a 30/60/90d soak window before allocating real capital. Drop wallets that turn negative.
- **Per-wallet bankroll allocation**, not flat $5/BUY. Allocate proportional to recent measured edge.
- **Conservative slippage assumption**: 100 bps off mirror fills. Most of the +$4,099 evaporates at realistic execution.

## Comparison to the live engine right now

From soak-analysis at session start (last 24h):
- Live `fade` strategy: 37 closes, **16% win rate**, −$11.37
- Live `adaptive_v2`: 6 closes, 17% win rate, −$1.32

Both production strategies are net-negative in the current window. The suggestion engine flagged `paper_stop_loss` as the dominant loss source (38% of closes, avg −$0.90) and recommended either tightening the entry gate or **flipping the thesis** (mirror `adaptive_v2_invert` / `quant_invert_drift` pattern). The fact that higher-edge buckets (`[0.15, 0.20)`) are 0% win rate is the loudest signal — the scorer is reliably wrong on conviction.

Three concrete next actions, ranked by my confidence:

1. **Investigate fade-invert** (high confidence the existing thesis is mis-signed for the current regime; cheap to test by toggling an existing flag, no new code needed).
2. **Build a cohort-persistence study for copy-trading** (1-2h of code, no engine changes; tracks daily leaderboard snapshots for 30 days). Defer the Polygon RPC sidecar until that lands.
3. **Stop investing time in hedged-pair**. The 14d and 3d backtests agree: it's a losing strategy on this engine's data, regardless of parameter tuning.

## Artifacts (this session)

- [`scripts/copy_mirror_sim.py`](copy_mirror_sim.py) — new mirror simulator
- [`scripts/copy_mirror_sim.md`](copy_mirror_sim.md) — full per-wallet breakdown
- [`scripts/_copy_activity_cache/`](_copy_activity_cache/) — per-wallet activity cache (20 wallets, ~40K records)
- [`scripts/_copy_market_price_cache.json`](_copy_market_price_cache.json) — market resolution cache (post-fix)
- [`scripts/hedged_pair_backtest_trades.jsonl`](hedged_pair_backtest_trades.jsonl) — 3-day re-run

## Open questions (for when you're back)

1. **Should I build the cohort-persistence study?** It's the cleanest next step before any sidecar code. Adds a daily cron-style script that snapshots the leaderboard.
2. **Should I run a fade-invert paper soak experiment?** Cheap — one settings flip and watch for a few hours.
3. **Should I keep the mirror sim around as a recurring experiment?** It's a nice "what would copy-trading have done" weekly check-in; not as a real strategy, but as a benchmark.
