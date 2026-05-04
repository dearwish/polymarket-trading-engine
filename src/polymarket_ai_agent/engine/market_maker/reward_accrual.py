"""Time-in-band reward accrual for resting market-maker quotes.

Polymarket pays a daily USDC subsidy to makers whose quotes rest inside
the reward band around the mid. The continuous nature of the subsidy
(USD/day, paid pro-rata for time spent in-band) doesn't fit cleanly into
the existing fill-based PnL bookkeeping — a $1000 quote that rests for
2 hours in-band on a market paying $1250/day deserves credit for ~$1.30
of daily yield even if it never fills.

This module owns:

- :class:`QuoteAccrualState`: per-quote in-memory tracker. The daemon
  keeps one of these per ``(strategy_id, market_id, side)`` slot for
  the lifetime of each MM rest.
- :func:`accrue`: pure-function update step. Given the state and the
  per-day reward yield at the current quote location, advances the
  state by ``elapsed_seconds`` and returns the USD accrued THIS PERIOD.
  The caller is responsible for persisting the period accrual via
  ``PortfolioEngine.record_reward_accrual`` and refreshing
  ``state.last_check_at``.

Out-of-band periods are tracked separately so the operator dashboard
can surface "this quote spent 60% of its life out of the reward band"
— that's the diagnostic for "the mid drifted past our quote and the
hysteresis kept us at a stale price for too long".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class QuoteAccrualState:
    """In-memory accrual tracker for one resting MM quote.

    All fields except ``placed_at`` are mutated by :func:`accrue` on
    each tick. ``cumulative_reward_usd`` is the running total of USD
    earned for this quote's full lifetime — it includes already-persisted
    amounts and not-yet-persisted residuals; the daemon uses
    :attr:`pending_reward_usd` to know how much still needs to be flushed
    to the DB.
    """

    placed_at: datetime
    last_check_at: datetime
    in_band_seconds: float = 0.0
    out_band_seconds: float = 0.0
    cumulative_reward_usd: float = 0.0
    pending_reward_usd: float = 0.0
    # Last computed daily reward rate at this quote — surfaced in the
    # heartbeat so operators can see "if this quote stays here for 24h
    # at the current book competition, it earns $X/day".
    last_daily_rate_usd: float = 0.0


def accrue(
    state: QuoteAccrualState,
    *,
    now: datetime,
    daily_reward_usd_at_quote: float,
    in_band: bool,
) -> float:
    """Advance ``state`` by the elapsed time since ``state.last_check_at``.

    Returns the USD accrued **this period only** (zero when out-of-band).
    Mutates the state in-place — the caller doesn't need to re-assign.
    Always advances ``last_check_at`` to ``now`` so a malformed earlier
    state can't cause runaway double-counting on the next call.

    ``daily_reward_usd_at_quote`` is the per-day yield the
    :func:`engine.maker_rewards.estimate_reward_for_size` function
    returns for our quote against the current book. The caller computes
    it once per tick and passes it here; this module doesn't reach into
    the book parser.
    """
    elapsed = (now - state.last_check_at).total_seconds()
    state.last_check_at = now
    if elapsed <= 0:
        # Clock skew or duplicate tick — refuse to accrue but keep state.
        return 0.0
    state.last_daily_rate_usd = max(0.0, float(daily_reward_usd_at_quote))
    if not in_band or daily_reward_usd_at_quote <= 0.0:
        state.out_band_seconds += elapsed
        return 0.0
    state.in_band_seconds += elapsed
    accrued = float(daily_reward_usd_at_quote) * elapsed / 86_400.0
    if accrued <= 0.0:
        return 0.0
    state.cumulative_reward_usd += accrued
    state.pending_reward_usd += accrued
    return accrued


def take_pending(state: QuoteAccrualState) -> float:
    """Return ``state.pending_reward_usd`` and reset it to zero.

    The daemon calls this when it's about to persist the pending amount
    via ``PortfolioEngine.record_reward_accrual``. Splitting the read +
    reset lets the caller emit a single DB row per persistence cycle
    instead of one row per tick.
    """
    pending = state.pending_reward_usd
    state.pending_reward_usd = 0.0
    return pending
