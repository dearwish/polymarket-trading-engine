"""Two-sided market-making strategy.

A passive liquidity-providing strategy that posts simultaneous YES-buy and
NO-buy resting limits around the book mid, captures the bid/ask spread when
both legs fill, and earns Polymarket maker-reward subsidies on markets
that pay them. Inventory-aware quote skew nudges fills back toward neutral
when one side gets ahead of the other.

Modules:

- :mod:`quoter` — pure-function quote pricing (mid + half-spread + skew).
- :mod:`inventory` — net-inventory math, derived from per-strategy positions.
- :mod:`scorer` — :class:`MarketMakerScorer`, the per-tick gating decision.

The daemon's ``_handle_market_maker_strategy`` lifecycle owns the actual
two-sided quote placement and per-leg fill bookkeeping; this package owns
all the math.
"""
from __future__ import annotations

from polymarket_trading_engine.engine.market_maker.inventory import (
    InventorySnapshot,
    compute_inventory,
)
from polymarket_trading_engine.engine.market_maker.quoter import (
    QuotePair,
    compute_quote_pair,
)
from polymarket_trading_engine.engine.market_maker.reward_accrual import (
    QuoteAccrualState,
    accrue,
    take_pending,
)
from polymarket_trading_engine.engine.market_maker.scanner import score_mm_market
from polymarket_trading_engine.engine.market_maker.scorer import (
    MARKET_MAKER_STRATEGY_TAG,
    MarketMakerScorer,
)

__all__ = [
    "InventorySnapshot",
    "MARKET_MAKER_STRATEGY_TAG",
    "MarketMakerScorer",
    "QuotePair",
    "QuoteAccrualState",
    "accrue",
    "compute_inventory",
    "compute_quote_pair",
    "score_mm_market",
    "take_pending",
]
