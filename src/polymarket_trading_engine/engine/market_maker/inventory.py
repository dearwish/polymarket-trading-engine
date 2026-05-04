"""Per-market inventory accounting for the market-maker strategy.

The MM strategy can hold multiple open positions on the same market
simultaneously — typically a YES position from a buy-bid fill and a NO
position from the symmetric buy-NO fill. The single-position-per-strategy
invariant other scorers rely on doesn't apply here.

Net inventory in dollar terms is the imbalance between the two legs:

    net_yes_usd = sum(YES position size_usd) − sum(NO position size_usd)

This is a coarse but stable signal for quote skew. A precise P&L-aware
exposure would weight each position by its mark-to-mid, but that adds a
moving target on every tick and the resulting skew oscillates as the mid
drifts; the dollar imbalance is constant per fill and behaves well as a
control signal.

The normalised ``skew`` returned by :func:`compute_inventory` is bounded
to ``[-1, 1]`` by the operator-tunable ``mm_max_inventory_usd`` cap and
fed straight into :func:`engine.market_maker.quoter.compute_quote_pair`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from polymarket_trading_engine.types import PositionRecord, SuggestedSide


@dataclass(slots=True, frozen=True)
class InventorySnapshot:
    """Net inventory + per-leg halt flags for one market at one tick."""

    yes_exposure_usd: float
    no_exposure_usd: float
    net_yes_usd: float
    skew: float
    halt_yes_buy: bool
    halt_no_buy: bool


def compute_inventory(
    open_positions: Iterable[PositionRecord],
    *,
    market_id: str,
    max_inventory_usd: float,
) -> InventorySnapshot:
    """Compute the inventory snapshot for ``market_id`` from open positions.

    ``open_positions`` is the strategy's slice of the position table —
    typically the result of ``portfolio.list_open_positions(strategy_id=
    "market_maker")``. Positions for other markets in the iterable are
    silently ignored so the caller can pass the un-filtered list.

    Halt rules: when the YES-side exposure already exceeds
    ``max_inventory_usd`` we stop buying more YES (``halt_yes_buy=True``)
    but keep posting the NO-buy leg so a fill flattens us. Mirror logic
    for the NO side. This is the inventory-cap escape hatch that prevents
    runaway one-sided accumulation when the market keeps trading in one
    direction.
    """
    yes_exposure = 0.0
    no_exposure = 0.0
    for pos in open_positions:
        if pos.market_id != market_id:
            continue
        if pos.side is SuggestedSide.YES:
            yes_exposure += float(pos.size_usd)
        elif pos.side is SuggestedSide.NO:
            no_exposure += float(pos.size_usd)

    net_yes_usd = yes_exposure - no_exposure
    cap = max(float(max_inventory_usd), 1e-9)
    skew_raw = net_yes_usd / cap
    skew = max(-1.0, min(1.0, skew_raw))

    # Halt the side that's already at-or-over the cap. We compare the
    # one-sided exposure against the cap rather than the net; a +5 / -3
    # net of +2 with a cap of 4 means YES is at 5 (over cap) and NO is at
    # 3 (under cap), so we should halt YES-buys but keep NO-buys flowing
    # to flatten.
    halt_yes = max_inventory_usd > 0.0 and yes_exposure >= max_inventory_usd
    halt_no = max_inventory_usd > 0.0 and no_exposure >= max_inventory_usd

    return InventorySnapshot(
        yes_exposure_usd=yes_exposure,
        no_exposure_usd=no_exposure,
        net_yes_usd=net_yes_usd,
        skew=skew,
        halt_yes_buy=halt_yes,
        halt_no_buy=halt_no,
    )
