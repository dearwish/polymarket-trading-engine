"""Paper-mode maker-order lifecycle.

Phase 3 of the adaptive-regime branch. The existing paper execution
engine fills every approved order immediately at a book-walk VWAP —
that's correct for taker entries but wrong for a "follow the crowd via
a resting limit" strategy where the whole point is to NOT fill unless
the market gives us a better price.

This module provides the lifecycle primitives that let the daemon
simulate resting limit orders:

- :class:`PaperMakerOrder` is the pending-order record.
- :func:`check_fill` answers "would this rest have been hit by the
  current book state?" using only live order-book features, no
  persistence.
- :func:`is_expired` returns True once the order has sat beyond its TTL.

The daemon owns the collection of pending orders (one per
(strategy_id, market_id) slot) and drives the state machine via these
helpers on every tick.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from polymarket_trading_engine.types import SuggestedSide


@dataclass(slots=True, frozen=True)
class PaperMakerOrder:
    """A resting limit order placed in paper mode.

    ``limit_price`` is quoted in the token's own frame — for a YES buy
    it's the YES price, for a NO buy it's the NO price. The fill check
    compares this to the opposite side's ask (since resting a bid at
    68¢ means we get filled when someone sells to us at ≤ 68¢, which
    surfaces as the ask dropping into our bid).
    """

    strategy_id: str
    market_id: str
    side: SuggestedSide
    limit_price: float
    size_usd: float
    placed_at: datetime
    ttl_seconds: int


def check_fill(
    order: PaperMakerOrder,
    ask_yes: float,
    ask_no: float,
) -> bool:
    """Return True when the current book would fill ``order`` on a taker
    crossing into our resting price.

    We're buying — YES or NO — so a fill happens when the opposite side
    of the spread (the ask on our token) drops to at-or-below our limit.
    ``ask > 0`` is required because a zero/missing ask just means the
    book isn't populated yet, not that the order filled.
    """
    if order.side is SuggestedSide.YES:
        ask = ask_yes
    elif order.side is SuggestedSide.NO:
        ask = ask_no
    else:
        return False
    return ask > 0.0 and ask <= order.limit_price


def is_expired(order: PaperMakerOrder, now: datetime) -> bool:
    """TTL-based cancel: return True once the rest has outlived its
    window without a fill. Discount-from-mid orders are meant to catch
    a brief pullback inside a trend; if the pullback doesn't happen
    inside the TTL the setup is stale and we'd rather start fresh than
    keep the old quote hanging out.
    """
    return now - order.placed_at >= timedelta(seconds=order.ttl_seconds)


def maker_limit_price(
    side: SuggestedSide,
    bid_yes: float,
    ask_yes: float,
    bid_no: float,
    ask_no: float,
    discount_bps: float,
) -> float:
    """Compute the limit price for a follow-with-maker order.

    We place the order a configurable ``discount_bps`` below the mid of
    our side's book — aggressive enough that a minor pullback catches
    us, passive enough that the typical taker flow doesn't. Returns 0.0
    when the book is too thin to price honestly (caller should skip).
    """
    if side is SuggestedSide.YES:
        bid, ask = bid_yes, ask_yes
    elif side is SuggestedSide.NO:
        bid, ask = bid_no, ask_no
    else:
        return 0.0
    if bid <= 0.0 or ask <= 0.0 or bid >= ask:
        return 0.0
    mid = (bid + ask) / 2.0
    # Discount is applied to the mid so it's symmetric across sides.
    price = mid * (1.0 - discount_bps / 10_000.0)
    # Clamp to a minimum sensible price so we never price below the
    # book's floor; one tick above the best bid is the natural limit
    # for a passive order that shouldn't cross into a taker.
    return max(0.01, min(0.99, price))
