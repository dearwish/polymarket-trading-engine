# Hedged-Pair (Gabagool) Backtest — Phase 1 Verdict

**Date:** 2026-05-13
**Plan:** `~/.claude/plans/scalable-drifting-pie.md`
**Outcome:** **KILL-CRITERION TRIGGERED. Phase 2 is not pursued.**

## Headline

The hedged-pair "gabagool" strategy as described in the external write-up is **unprofitable on this engine's existing soak data**, across every plausible parameter combination tested. The dominant failure mode is **single-leg fill exposure** — one side of the pair fills, the other never dips, and the forced flatten on the open leg destroys the per-pair edge.

## Coverage

- Window: 7 and 14 days of `logs/events.jsonl`
- Source ticks: 103,248 `daemon_tick` events under `strategy_id=fade` (the canonical universe of BTC 15m candle markets observed by the soak)
- Resolutions: 265 markets resolved via gamma-api.polymarket.com (1 transient timeout, cached for reruns)

## Parameter sweep

| Window | pair-cost-max | maker-offset | TTL (s) | Sizing | Signals | Pair-fill % | Paired WR | Single-leg | Mean per pair ($) | **Net P&L ($)** |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| 7d | 0.98 | 0.01 | 120 | symmetric | 16 | 37.5% | 60.0% | 9 | +0.44 | **−5.02** |
| 7d | 0.98 | 0.01 | 60 | symmetric | 16 | 25.0% | 75.0% | 11 | +0.82 | **−3.66** |
| 7d | 0.98 | 0.01 | 300 | symmetric | 16 | 56.2% | 57.1% | 6 | +0.23 | **−7.73** |
| 7d | 0.98 | 0.01 | 120 | asymmetric | 16 | 37.5% | 60.0% | 9 | +0.87 | **−2.36** |
| 14d | 0.99 | 0.01 | 120 | symmetric (API) | 266 | 62.0% | 47.9% | 98 | +0.05 | **−70.97** |
| 14d | 0.99 | 0.01 | 120 | asymmetric | 265 | ~62% | ~46% | ~97 | ~+0.07 | **~−65** |
| 14d | 0.97 | 0.01 | 120 | symmetric | 16 | 37.5% | 33.3% | 9 | −0.14 | **−7.42** |

Every single combination produces net negative P&L. Tightening the threshold (the article's "real arb" zone of `pair_cost < 0.97`) yields a *worse* paired-trade win rate (33%) — the rare cheap-pair-cost ticks are not predictive of profitable pair completions, they're indicative of one-sided book dislocations that resolve adversely.

## Root cause

The article's promise — "buy YES+NO at sum < $1, hold to resolution, profit guaranteed" — relies on **simultaneous fills at the visible cheap-tick prices**. The conservative `maker_through` fill model (used here and in `penny_maker_backtest.py`) correctly does not give credit for fills we can't actually achieve as passive makers without a queue-position guarantee.

What actually happens in the data:

1. A pair-eligible signal fires when both `ask_yes` and `ask_no` are transiently cheap.
2. Posting maker bids 1¢ below those asks captures the *opportunity* to fill, but the ask of the side that triggered the dip recovers quickly (it was the dipping side; the dip is over).
3. The OTHER side's ask — which was already cheap *because the market was pricing in BTC direction* — keeps drifting away from our bid.
4. Result: one leg fills (the one that already moved), one leg doesn't (the one that was just slightly cheap on the spread). We're left with single-sided directional exposure, and the flatten-at-market exit eats most of the profit on the trades that did pair up.

The article's "98% win rate" and "$58 in 15 minutes" claims appear to require either (a) sub-100ms taker execution that the engine is not built for, or (b) measurement against unrealistic fill assumptions (touch fills, no queue contention). Neither survives the maker-through test on real data.

## Why this contradicts the analogous MM module's reward economics

The existing `market_maker` strategy uses the same two-sided maker quoting mechanic and is profitable in soak — but it earns the **daily reward subsidy** that Polymarket pays for resting inside the reward band. Strip out the subsidy (which `pair_mode` does by definition — gabagool ignores the reward band), and the raw spread capture on BTC 15m markets is not enough to overcome the single-leg failure rate. This is why MM works on reward-paying markets (sports/politics with rewardsDailyRate > 0) and not on the rolling crypto candle markets the article targets.

## Decision

Per the approved plan's explicit kill-criterion:

> *"if backtested net P&L (post-fees) is below the existing penny strategy on the same window, do not proceed to Phase 2."*

Net P&L is **negative** in every configuration. Phase 2 (MM `pair_mode` extension + `hedged_pair` strategy registration) is **not implemented**.

## What would change the verdict

These are NOT being pursued — listed only so the decision is reproducible:

1. **Sub-100ms taker arb infrastructure.** Cross the ask on both sides instantaneously when `pair_cost < threshold`. Requires colocated WS + Polygon co-location + raw `eth_sendRawTransaction` direct submission. The article's profitable bots appear to do this.
2. **Reward-band-overlap markets.** Run pair_mode only on markets that ALSO pay maker rewards. Gabagool + MM in one strategy — but at that point we're already doing MM and the marginal gabagool gate is value-destroying noise.
3. **Different market family.** Politics/sports binaries with thicker books and slower price discovery may show wider pair-cost dispersion and longer-lived opportunities. Out of scope for this plan; would require a separate discovery phase first.

None of these are budgeted in the current plan. The honest call is to stop and redirect to Phase 4.

## Artifacts

- [`scripts/hedged_pair_backtest.py`](hedged_pair_backtest.py) — the backtest
- [`scripts/hedged_pair_backtest.md`](hedged_pair_backtest.md) — latest run report (parameters of the final run)
- [`scripts/hedged_pair_backtest_trades.jsonl`](hedged_pair_backtest_trades.jsonl) — per-trade detail of the latest run
- [`scripts/_hedged_pair_resolution_cache.json`](_hedged_pair_resolution_cache.json) — gamma-api resolution cache (265 markets)
