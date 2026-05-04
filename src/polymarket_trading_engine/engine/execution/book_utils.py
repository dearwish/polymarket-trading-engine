"""Order-book helpers shared across the execution layer.

Currently exposes :func:`first_level_with_size`, which collapses a sorted
price-priority ladder down to the first level whose size is big enough to
be a "real" quote. Used by the follow-with-maker path to avoid anchoring
our limit on a ghost 1-lot level — the reference market-maker repo
(``gamma-trade-lab/polymarket-market-maker``) applies the same idea in
``find_best_price_with_size``.
"""
from __future__ import annotations


def first_level_with_size(
    levels: list[tuple[float, float]],
    min_size: float,
) -> float | None:
    """Return the price of the first level in ``levels`` whose size is
    at least ``min_size``, or ``None`` if no level qualifies.

    ``levels`` is expected to arrive in price-priority order — bids
    high-to-low, asks low-to-high. This matches
    :meth:`engine.market_state.OrderBookSide.sorted_levels`, so callers
    can pass its output directly for either side. ``min_size <= 0``
    short-circuits to the top level (or ``None`` when empty), preserving
    "no filter" semantics without forcing a special case at the call site.
    """
    if not levels:
        return None
    if min_size <= 0.0:
        return levels[0][0]
    for price, size in levels:
        if size >= min_size:
            return price
    return None
