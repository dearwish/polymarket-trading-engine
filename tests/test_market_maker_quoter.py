"""Pure-math unit tests for the market-maker quote pricer.

Locks in:
- the ``mid - half_spread`` / ``(1 - mid) - half_spread`` formula,
- the inventory-skew direction (long YES → lower yes_bid, higher no_bid),
- the abstain-on-thin-book contract,
- the inventory halt flag short-circuit (``halt_*`` → ``None`` quote).
"""
from __future__ import annotations

from polymarket_trading_engine.engine.market_maker.quoter import (
    QuotePair,
    compute_quote_pair,
    in_reward_band,
)


def _quote(**kwargs) -> QuotePair:
    defaults = dict(
        bid_yes=0.50,
        ask_yes=0.54,
        half_spread=0.02,
        skew=0.0,
        skew_strength=0.5,
        halt_yes_buy=False,
        halt_no_buy=False,
    )
    defaults.update(kwargs)
    return compute_quote_pair(**defaults)


def test_symmetric_quote_at_mid_with_zero_skew() -> None:
    """Mid 0.52, half-spread 0.02, skew 0 → yes_bid=0.50, no_bid=0.46."""
    q = _quote(bid_yes=0.50, ask_yes=0.54, half_spread=0.02, skew=0.0)
    assert abs(q.mid_yes - 0.52) < 1e-9
    assert q.yes_bid is not None and abs(q.yes_bid - 0.50) < 1e-9
    assert q.no_bid is not None and abs(q.no_bid - 0.46) < 1e-9
    assert q.skew == 0.0


def test_long_yes_inventory_lowers_yes_bid_and_raises_no_bid() -> None:
    """Positive skew (long YES) should make us less aggressive on YES-buys
    and more aggressive on NO-buys, both by ``skew_strength × half_spread``.
    """
    base = _quote(skew=0.0)
    long = _quote(skew=1.0, skew_strength=0.5)
    # With skew_strength=0.5, half_spread=0.02 → offset = 0.01
    assert abs(long.yes_bid - (base.yes_bid - 0.01)) < 1e-9
    assert abs(long.no_bid - (base.no_bid + 0.01)) < 1e-9


def test_short_yes_inventory_mirrors_skew_direction() -> None:
    """Negative skew (long NO) should mirror: raise yes_bid, lower no_bid."""
    base = _quote(skew=0.0)
    short = _quote(skew=-1.0, skew_strength=0.5)
    assert abs(short.yes_bid - (base.yes_bid + 0.01)) < 1e-9
    assert abs(short.no_bid - (base.no_bid - 0.01)) < 1e-9


def test_skew_is_clamped_to_unit_interval() -> None:
    """A runaway skew of 5.0 must not push prices off the grid — the
    quoter caps the input at ±1.0 before applying.
    """
    capped = _quote(skew=5.0, skew_strength=0.5)
    bounded = _quote(skew=1.0, skew_strength=0.5)
    assert capped.yes_bid == bounded.yes_bid
    assert capped.no_bid == bounded.no_bid
    assert capped.skew == 1.0


def test_one_sided_book_returns_none_quotes() -> None:
    """A missing ask (or crossed book) means no mid; the quoter must
    abstain entirely so the caller can skip this tick.
    """
    one_sided = _quote(bid_yes=0.50, ask_yes=0.0)
    assert one_sided.yes_bid is None
    assert one_sided.no_bid is None
    crossed = _quote(bid_yes=0.55, ask_yes=0.50)
    assert crossed.yes_bid is None
    assert crossed.no_bid is None


def test_halt_yes_buy_zeroes_yes_leg_keeps_no_leg() -> None:
    """When the YES-side inventory cap fires, the quoter still returns
    a NO-buy price — the cap is supposed to flatten, not freeze.
    """
    q = _quote(skew=1.0, halt_yes_buy=True, halt_no_buy=False)
    assert q.yes_bid is None
    assert q.no_bid is not None


def test_halt_no_buy_zeroes_no_leg_keeps_yes_leg() -> None:
    q = _quote(skew=-1.0, halt_yes_buy=False, halt_no_buy=True)
    assert q.no_bid is None
    assert q.yes_bid is not None


def test_extreme_mid_clamps_quote_to_valid_range() -> None:
    """Very-low mid (e.g. 0.04) with half_spread 0.02 would push
    yes_bid below the 0.01 floor; the quote must clamp, not error.
    """
    q = _quote(bid_yes=0.03, ask_yes=0.05, half_spread=0.02, skew=0.0)
    assert q.yes_bid is not None
    assert q.yes_bid >= 0.01
    assert q.no_bid is not None
    assert q.no_bid <= 0.99


def test_in_reward_band_passes_when_quote_within_max_spread() -> None:
    """Reward band ±0.03 around mid 0.50 → quotes 0.47 and 0.53 are inside."""
    assert in_reward_band(0.47, midpoint=0.50, rewards_max_spread_pct=3.0)
    assert in_reward_band(0.53, midpoint=0.50, rewards_max_spread_pct=3.0)
    # Just outside.
    assert not in_reward_band(0.46, midpoint=0.50, rewards_max_spread_pct=3.0)
    assert not in_reward_band(0.54, midpoint=0.50, rewards_max_spread_pct=3.0)


def test_in_reward_band_passes_when_no_band_configured() -> None:
    """rewards_max_spread_pct ≤ 0 means "no reward gate" — every quote qualifies."""
    assert in_reward_band(0.10, midpoint=0.50, rewards_max_spread_pct=0.0)
    assert in_reward_band(0.90, midpoint=0.50, rewards_max_spread_pct=-1.0)
