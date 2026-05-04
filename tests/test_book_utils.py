"""Tests for shared order-book helpers."""
from __future__ import annotations

from polymarket_trading_engine.engine.execution.book_utils import first_level_with_size


def test_empty_book_returns_none() -> None:
    assert first_level_with_size([], min_size=50.0) is None


def test_zero_threshold_returns_top_level() -> None:
    """min_size<=0 must short-circuit to the top level so the call site
    can swap 'filter off' and 'filter on' without branching."""
    levels = [(0.62, 1.0), (0.61, 500.0)]
    assert first_level_with_size(levels, min_size=0.0) == 0.62


def test_negative_threshold_treated_as_zero() -> None:
    """Defensive: a negative threshold shouldn't skip the top level."""
    assert first_level_with_size([(0.50, 1.0)], min_size=-5.0) == 0.50


def test_skips_thin_levels_until_qualifying_size() -> None:
    """Top two levels are ghost-sized; we want the first level where real
    liquidity rests. Bid ladder: high-to-low prices."""
    levels = [
        (0.68, 1.0),
        (0.67, 5.0),
        (0.66, 100.0),
        (0.65, 300.0),
    ]
    assert first_level_with_size(levels, min_size=50.0) == 0.66


def test_all_levels_thin_returns_none() -> None:
    """No level has enough size — caller should treat this as 'no real
    quote' and fall back to the raw best-price or abstain."""
    levels = [(0.68, 1.0), (0.67, 2.0), (0.66, 3.0)]
    assert first_level_with_size(levels, min_size=50.0) is None


def test_boundary_size_equal_to_threshold_qualifies() -> None:
    """``>=`` not ``>`` so an exactly-at-threshold level is accepted."""
    levels = [(0.68, 5.0), (0.67, 50.0)]
    assert first_level_with_size(levels, min_size=50.0) == 0.67
