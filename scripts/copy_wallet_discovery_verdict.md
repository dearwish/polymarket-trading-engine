# Copy-Trading Wallet Discovery — Phase 4 Verdict

**Date:** 2026-05-13
**Plan:** `~/.claude/plans/scalable-drifting-pie.md`
**Outcome:** **Tentative pass, but criteria are too weak to commit. A more rigorous follow-up is required before any sidecar work.**

## Headline

The mechanical 4/4 pass on the auto-criteria (median history 199d, 250 trades, +40.6% pnl/vol, 3% top-trade share for top-20 by composite score) is **not strong enough evidence to build a copy-trading sidecar**. Three serious confounders limit this signal:

1. **Survivorship bias.** The leaderboard is sorted by all-time realized PnL, so we are by construction looking at winners. The relevant question is whether *yesterday's top-20 are still in tomorrow's top-20*, which a one-shot snapshot can't answer.
2. **Page-cap artifacts.** Every top wallet returned exactly 250 trades — the API's pagination ceiling, not a measurement. PnL-per-volume is computed over whatever timeframe those 250 trades happen to span, which differs by wallet. Hit rates of 100% are suspicious and almost certainly an artifact of only fetching realized-winners pages or category truncation.
3. **Category classifier is too coarse.** The dominant category for most top wallets is `"other"` (e.g. 72% for `debased`, 99% for one of the wallets), meaning my regex didn't match the actual slug structure. Diversification scoring is therefore unreliable in this run.

A proper Phase 4 needs at minimum: (a) cohort persistence analysis — pull leaderboard snapshots 30/60/90 days back and measure rank correlation, (b) larger `--max-pages` to capture full history, (c) a slug → category mapping driven by the actual `eventSlug` taxonomy rather than guessed regexes.

## Top-20 snapshot (composite score)

From [`scripts/copy_wallet_discovery_report.md`](copy_wallet_discovery_report.md) — for the historical record:

- Median composite score 9.30 / 10
- Median history 199 days (range 67–683)
- Median trade count 250 (page-cap; not informative)
- Median PnL/$volume +40.6%
- Median top-trade share 3% (genuinely low concentration — this is the strongest signal)

The wallets `bossoskil1`, `RN1`, `0x2a2c…9bc1` show very large dollar volumes ($45M–$120M) which makes them poor mirror candidates regardless of edge — their trades themselves move markets. A realistic copy-trading sidecar would need to filter to wallets with **smaller** per-trade sizes ($1k–$50k) where copies wouldn't suffer slippage.

## Recommendation

**Do not start the sidecar build yet.** Two follow-up tasks instead, both small:

1. **Cohort persistence analysis** (1-2h): pull leaderboard daily for 7 days starting now into [`scripts/_copy_wallet_cache/lb_<date>.json`](_copy_wallet_cache/). After 7 days, compare top-20 stability via rank correlation. If the top-20 churns by >50% week-over-week, the leaderboard signal is too unstable to mirror; if churn is <20%, there's structural alpha worth investigating.
2. **Slug-taxonomy enrichment** (1-2h): pull `eventSlug → category` mapping from gamma-api `/events` and reclassify. This is needed for any meaningful diversification analysis. The `"other"` blob hides real category concentrations.

Only after both land — and the cohort persistence shows real stability — should the plan progress to the actual Polygon RPC sidecar shape that was deferred in Phase 4.

## Artifacts

- [`scripts/copy_wallet_discovery.py`](copy_wallet_discovery.py) — discovery script
- [`scripts/copy_wallet_discovery_report.md`](copy_wallet_discovery_report.md) — auto-generated ranking + auto-verdict (mechanical)
- [`scripts/copy_wallet_discovery.csv`](copy_wallet_discovery.csv) — per-wallet metrics in CSV
- [`scripts/_copy_wallet_cache/`](_copy_wallet_cache/) — per-wallet closed-positions cache (50 wallets fetched)
