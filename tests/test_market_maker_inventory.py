"""Pure-math tests for the MM inventory snapshot.

Verifies the YES-vs-NO exposure tally, the per-side halt-cap flag, and
that positions for OTHER markets in the iterable are silently ignored
(so the daemon can pass the un-filtered ``list_open_positions`` result).
"""
from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trading_engine.engine.market_maker.inventory import compute_inventory
from polymarket_trading_engine.types import PositionRecord, SuggestedSide


def _pos(
    market_id: str = "m",
    side: SuggestedSide = SuggestedSide.YES,
    size_usd: float = 1.0,
    entry_price: float = 0.50,
    order_id: str = "x",
) -> PositionRecord:
    return PositionRecord(
        market_id=market_id,
        side=side,
        size_usd=size_usd,
        entry_price=entry_price,
        order_id=order_id,
        opened_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        strategy_id="market_maker",
    )


def test_empty_position_list_is_neutral() -> None:
    snap = compute_inventory([], market_id="m", max_inventory_usd=5.0)
    assert snap.yes_exposure_usd == 0.0
    assert snap.no_exposure_usd == 0.0
    assert snap.net_yes_usd == 0.0
    assert snap.skew == 0.0
    assert not snap.halt_yes_buy
    assert not snap.halt_no_buy


def test_yes_only_positions_produce_positive_skew() -> None:
    legs = [_pos(side=SuggestedSide.YES, size_usd=2.0)]
    snap = compute_inventory(legs, market_id="m", max_inventory_usd=5.0)
    assert snap.yes_exposure_usd == 2.0
    assert snap.no_exposure_usd == 0.0
    assert snap.net_yes_usd == 2.0
    assert snap.skew == 0.4  # 2 / 5


def test_no_only_positions_produce_negative_skew() -> None:
    legs = [_pos(side=SuggestedSide.NO, size_usd=3.0)]
    snap = compute_inventory(legs, market_id="m", max_inventory_usd=5.0)
    assert snap.no_exposure_usd == 3.0
    assert snap.skew == -0.6


def test_yes_exposure_at_cap_halts_yes_buys_only() -> None:
    """When the YES leg already at-or-over the cap, halt YES-buy but
    keep NO-buy flowing so the next NO fill flattens us.
    """
    legs = [_pos(side=SuggestedSide.YES, size_usd=5.0)]
    snap = compute_inventory(legs, market_id="m", max_inventory_usd=5.0)
    assert snap.halt_yes_buy is True
    assert snap.halt_no_buy is False


def test_skew_clamped_to_unit_interval_when_exposure_exceeds_cap() -> None:
    """yes_exposure of 20 with cap 5 → raw skew 4.0; should clamp to +1.0."""
    legs = [_pos(side=SuggestedSide.YES, size_usd=20.0)]
    snap = compute_inventory(legs, market_id="m", max_inventory_usd=5.0)
    assert snap.skew == 1.0


def test_positions_for_other_markets_are_ignored() -> None:
    """The MM strategy may open positions on multiple markets; the
    inventory snapshot for market_id 'm1' must not see m2's exposure.
    """
    legs = [
        _pos(market_id="m1", side=SuggestedSide.YES, size_usd=2.0),
        _pos(market_id="m2", side=SuggestedSide.YES, size_usd=10.0),
    ]
    snap = compute_inventory(legs, market_id="m1", max_inventory_usd=5.0)
    assert snap.yes_exposure_usd == 2.0


def test_yes_and_no_positions_partially_offset() -> None:
    """A YES leg + smaller NO leg → small positive net YES exposure,
    moderate skew (depending on cap), neither halt fires.
    """
    legs = [
        _pos(side=SuggestedSide.YES, size_usd=3.0),
        _pos(side=SuggestedSide.NO, size_usd=1.0),
    ]
    snap = compute_inventory(legs, market_id="m", max_inventory_usd=5.0)
    assert snap.net_yes_usd == 2.0
    assert snap.skew == 0.4
    assert not snap.halt_yes_buy
    assert not snap.halt_no_buy


def test_zero_max_inventory_disables_halts_and_caps_skew() -> None:
    """``mm_max_inventory_usd`` of 0 opts out of the halt gate; the
    skew formula still produces a finite value via the ``max(cap, 1e-9)``
    guard, and the halt flags must stay False (no cap = no halt).
    """
    legs = [_pos(side=SuggestedSide.YES, size_usd=10.0)]
    snap = compute_inventory(legs, market_id="m", max_inventory_usd=0.0)
    assert snap.halt_yes_buy is False
    assert snap.halt_no_buy is False
    assert snap.skew == 1.0  # clamp catches the divide-by-zero blowup
