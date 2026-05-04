"""Market-selection ranking for the market-maker strategy.

The MM strategy's universe is fundamentally different from the BTC
short-horizon scorers — it needs reward-paying, liquid, slow-moving
markets where passive yield + spread capture compensate for adverse
selection. This module owns the **scoring math** for ranking candidates;
the actual API fetch + filtering lives in
:meth:`PolymarketConnector.discover_mm_markets` so it can stay close to
the rest of the connector's HTTP plumbing.

Score formula (geometric mean — replaces a linear yield/liquidity in 2026-05-03):

    score = sqrt(daily_rate) × sqrt(1000 / max(liquidity_usd, 1000))

The economics: a $1000 quote in a thin market captures a bigger
fraction of the per-band reward pool, so thin-low-paying markets DO
genuinely give more reward income per dollar than fat-high-paying ones
— the linear formula ``daily_rate × 1000 / liquidity`` was actually
correct in that ranking direction. The problem it caused was rank
COMPRESSION: a $100/day market with $5k liquidity scored 20× higher
than a $1250/day market with $700k liquidity, so the top-N pick would
saturate on the thinnest tier even when fatter (and more diverse)
markets were available.

The geometric-mean form preserves the thin-wins ordering but compresses
the gap — under the live 2026-05-03 soak the same comparison narrows
from 20× to ~3-4×, so the top-N gets a useful mix of yield tiers
instead of concentrating in one tier. This buys diversity at the cost
of a small expected-income reduction; net better for an unattended
soak that benefits from observing multiple market types.
"""
from __future__ import annotations

import math

from polymarket_trading_engine.types import MarketCandidate


def score_mm_market(candidate: MarketCandidate) -> float:
    """Return a nonnegative ranking score for ``candidate``.

    Returns ``0.0`` when the market doesn't pay rewards (the MM thesis
    requires them). The caller is responsible for the
    ``rewards_daily_rate > 0`` and ``liquidity_usd >= floor`` filters
    upstream of ranking — this function is a pure score, not a gate.
    """
    daily_rate = float(candidate.rewards_daily_rate or 0.0)
    if daily_rate <= 0.0:
        return 0.0
    liquidity = float(candidate.liquidity_usd or 0.0)
    # 1000 is the soft floor on liquidity so thin markets (< $1k) share
    # the same denominator and don't get artificially boosted relative
    # to markets just above the floor.
    denom = max(liquidity, 1000.0)
    return math.sqrt(daily_rate) * math.sqrt(1000.0 / denom)
