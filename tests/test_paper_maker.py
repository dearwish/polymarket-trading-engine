"""Unit tests for paper-mode maker-order primitives (phase 3 adaptive-regime)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trading_engine.engine.execution.paper_maker import (
    PaperMakerOrder,
    check_fill,
    is_expired,
    maker_limit_price,
)
from polymarket_trading_engine.types import SuggestedSide


def _now() -> datetime:
    return datetime(2026, 4, 22, 18, 0, 0, tzinfo=timezone.utc)


def _yes_order(limit: float = 0.68) -> PaperMakerOrder:
    return PaperMakerOrder(
        strategy_id="adaptive",
        market_id="m",
        side=SuggestedSide.YES,
        limit_price=limit,
        size_usd=2.0,
        placed_at=_now(),
        ttl_seconds=300,
    )


def test_check_fill_yes_triggers_when_ask_crosses() -> None:
    """A resting YES bid at 0.68 fills once the YES ask drops to 0.68 or
    below — that's a taker selling into our resting price.
    """
    order = _yes_order(limit=0.68)
    # Ask above our limit → no fill.
    assert check_fill(order, ask_yes=0.70, ask_no=0.30) is False
    # Ask right at the limit → filled.
    assert check_fill(order, ask_yes=0.68, ask_no=0.32) is True
    # Ask well below → filled (aggressive taker crossing through).
    assert check_fill(order, ask_yes=0.60, ask_no=0.40) is True


def test_check_fill_no_side_uses_no_ask() -> None:
    """A NO buy's fill is decided by the NO side's ask, not the YES side's."""
    order = PaperMakerOrder(
        strategy_id="adaptive",
        market_id="m",
        side=SuggestedSide.NO,
        limit_price=0.40,
        size_usd=2.0,
        placed_at=_now(),
        ttl_seconds=300,
    )
    assert check_fill(order, ask_yes=0.58, ask_no=0.42) is False
    assert check_fill(order, ask_yes=0.62, ask_no=0.40) is True


def test_check_fill_zero_ask_is_no_fill() -> None:
    """An empty book (ask = 0) must NOT register as a fill — a zero ask
    means the book isn't populated, not that the order filled.
    """
    order = _yes_order(limit=0.68)
    assert check_fill(order, ask_yes=0.0, ask_no=0.0) is False


def test_is_expired_by_ttl() -> None:
    order = _yes_order()
    # Just after placement: not expired.
    assert is_expired(order, _now() + timedelta(seconds=1)) is False
    # At TTL boundary: expired.
    assert is_expired(order, _now() + timedelta(seconds=300)) is True
    # Beyond TTL: expired.
    assert is_expired(order, _now() + timedelta(seconds=600)) is True


def test_maker_limit_price_applies_discount_below_mid_yes() -> None:
    """YES bid/ask = 0.60/0.62, 50 bps discount → limit = 0.61 * 0.995 = 0.60695.

    Verifies the formula: mid × (1 − discount_bps / 10_000).
    """
    price = maker_limit_price(
        SuggestedSide.YES,
        bid_yes=0.60,
        ask_yes=0.62,
        bid_no=0.38,
        ask_no=0.40,
        discount_bps=50.0,
    )
    assert abs(price - 0.60695) < 1e-6


def test_maker_limit_price_no_side_reads_no_book() -> None:
    """NO side pricing uses the NO book's bid/ask, not YES's."""
    price = maker_limit_price(
        SuggestedSide.NO,
        bid_yes=0.60,
        ask_yes=0.62,
        bid_no=0.38,
        ask_no=0.40,
        discount_bps=50.0,
    )
    # NO mid = (0.38 + 0.40) / 2 = 0.39; 0.39 * 0.995 = 0.38805
    assert abs(price - 0.38805) < 1e-6


def test_maker_limit_price_returns_zero_on_bad_book() -> None:
    """Missing ask or crossed book → 0.0, signalling the caller should skip."""
    # Missing ask
    assert maker_limit_price(
        SuggestedSide.YES,
        bid_yes=0.60,
        ask_yes=0.0,
        bid_no=0.0,
        ask_no=0.40,
        discount_bps=50.0,
    ) == 0.0
    # Crossed book (bid >= ask)
    assert maker_limit_price(
        SuggestedSide.YES,
        bid_yes=0.60,
        ask_yes=0.60,
        bid_no=0.40,
        ask_no=0.40,
        discount_bps=50.0,
    ) == 0.0


def test_maker_limit_price_clamps_to_valid_range() -> None:
    """Extreme inputs that would push the price outside [0.01, 0.99]
    must clamp so the produced limit is always a representable
    CLOB price.
    """
    price = maker_limit_price(
        SuggestedSide.YES,
        bid_yes=0.005,
        ask_yes=0.008,
        bid_no=0.0,
        ask_no=0.0,
        discount_bps=50.0,
    )
    assert price >= 0.01
