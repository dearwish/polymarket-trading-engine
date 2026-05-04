"""Pure-math tests for the per-quote reward-accrual tracker.

Locks in:

- USD-per-second math (``daily_rate × elapsed / 86400``)
- in-band vs out-of-band period accounting
- ``take_pending`` resets pending without touching cumulative
- clock-skew safety (negative elapsed → no-op)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trading_engine.engine.market_maker.reward_accrual import (
    QuoteAccrualState,
    accrue,
    take_pending,
)


def _state(at: datetime) -> QuoteAccrualState:
    return QuoteAccrualState(placed_at=at, last_check_at=at)


def test_accrue_in_band_credits_proportional_to_elapsed_time() -> None:
    """1 hour at $1250/day = $1250/24 ≈ $52.08 expected."""
    t0 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    state = _state(t0)
    earned = accrue(
        state,
        now=t0 + timedelta(hours=1),
        daily_reward_usd_at_quote=1250.0,
        in_band=True,
    )
    expected = 1250.0 / 24.0
    assert abs(earned - expected) < 1e-9
    assert abs(state.cumulative_reward_usd - expected) < 1e-9
    assert abs(state.pending_reward_usd - expected) < 1e-9
    assert state.in_band_seconds == 3600.0
    assert state.out_band_seconds == 0.0


def test_accrue_out_of_band_credits_zero_but_advances_time() -> None:
    """When the mid drifts past our quote we earn nothing, but the
    out-band timer must still advance so the operator can see it.
    """
    t0 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    state = _state(t0)
    earned = accrue(
        state,
        now=t0 + timedelta(minutes=30),
        daily_reward_usd_at_quote=500.0,
        in_band=False,
    )
    assert earned == 0.0
    assert state.cumulative_reward_usd == 0.0
    assert state.pending_reward_usd == 0.0
    assert state.in_band_seconds == 0.0
    assert state.out_band_seconds == 1800.0


def test_accrue_zero_rate_in_band_credits_zero() -> None:
    """In-band but daily_rate=0 (book is fully crowded so our share is
    zero) must accrue zero — the in_band flag alone shouldn't credit.
    """
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    state = _state(t0)
    earned = accrue(state, now=t0 + timedelta(seconds=600), daily_reward_usd_at_quote=0.0, in_band=True)
    assert earned == 0.0
    assert state.in_band_seconds == 0.0
    # The out-band timer DOES advance since rate=0 means we're not
    # actually contributing to the pool (functionally identical to
    # being out of band as far as accrual is concerned).
    assert state.out_band_seconds == 600.0


def test_accrue_advances_last_check_at_so_subsequent_calls_chain() -> None:
    """Two sequential 30-min accruals should sum to 1 hour, not collapse."""
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    state = _state(t0)
    accrue(state, now=t0 + timedelta(minutes=30), daily_reward_usd_at_quote=1000.0, in_band=True)
    accrue(state, now=t0 + timedelta(minutes=60), daily_reward_usd_at_quote=1000.0, in_band=True)
    assert state.in_band_seconds == 3600.0
    expected = 1000.0 / 24.0
    assert abs(state.cumulative_reward_usd - expected) < 1e-6


def test_negative_elapsed_is_a_noop() -> None:
    """Clock skew (or a duplicate tick) shouldn't credit anything."""
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    state = _state(t0 + timedelta(seconds=10))  # last_check is in the future
    earned = accrue(state, now=t0, daily_reward_usd_at_quote=1000.0, in_band=True)
    assert earned == 0.0
    assert state.cumulative_reward_usd == 0.0
    assert state.in_band_seconds == 0.0


def test_take_pending_resets_pending_keeps_cumulative() -> None:
    """``take_pending`` is the daemon's flush point — the pending
    counter resets to zero so the next persistence cycle doesn't
    double-credit, but the lifetime cumulative stays.
    """
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    state = _state(t0)
    accrue(state, now=t0 + timedelta(seconds=86400), daily_reward_usd_at_quote=240.0, in_band=True)
    assert abs(state.cumulative_reward_usd - 240.0) < 1e-9
    drained = take_pending(state)
    assert abs(drained - 240.0) < 1e-9
    assert state.pending_reward_usd == 0.0
    # Cumulative is the lifetime invariant, unaffected by the drain.
    assert abs(state.cumulative_reward_usd - 240.0) < 1e-9
