"""Expected maker-reward yield for a Polymarket CLOB quote.

Polymarket pays a daily USDC subsidy to makers whose quotes sit inside a
narrow band around the mid. The per-market parameters are:

- ``rewards.rewards_daily_rate``: total USDC/day paid on this market
- ``rewards.max_spread``: half-width of the reward band, expressed as a
  percentage (e.g. ``3.0`` means ±3 cents around mid)
- ``rewards.min_size``: minimum order size to be eligible

The daily pool is split equally between the bid side and the ask side.
Within a side, the pool is distributed in proportion to each resting
level's ``Q = S × size`` where::

    s = |price - mid|
    S = ((v - s) / v)² if s <= v else 0        # v = max_spread / 100
    size = existing_size_at_level + your_added_size

For a passive maker posting ``$100`` at a single level, ``added_size`` in
shares is ``100 / price``. This module's :func:`estimate_reward_per_100`
returns the expected daily USDC reward for exactly that $100 quote given
the current book state on that side — the same quantity the
`gamma-trade-lab/polymarket-market-maker` reference repo uses to score
candidate markets before allocating capital.

Formula reference:
  https://docs.polymarket.com/#rewards
  github.com/gamma-trade-lab/polymarket-market-maker data_updater/find_markets.py

Scope: pure-math module. No I/O, no connector dependency. The caller
owns extracting ``rewards_daily_rate`` / ``max_spread`` / book snapshot
from a :class:`MarketCandidate` + :class:`OrderBookSnapshot`.
"""
from __future__ import annotations


def estimate_reward_for_size(
    *,
    target_price: float,
    midpoint: float,
    book_levels: list[tuple[float, float]],
    max_spread_pct: float,
    daily_reward_usd: float,
    size_usd: float,
) -> float:
    """Return the expected daily USDC reward for posting ``size_usd`` at
    ``target_price``.

    Same shape function as :func:`estimate_reward_per_100`, parametric on
    quote size. Used by the market-maker strategy where leg sizes can be
    $5 (paper-soak validation) up to $1000+ (live, sports markets where
    Polymarket's ``rewardsMinSize`` is 1000 USDC).

    ``book_levels`` is the list of existing ``(price, size)`` tuples on
    the SAME side as ``target_price`` — bids if we'd rest a buy, asks
    for a sell. Levels outside the reward band are silently ignored;
    the caller can pass the full side without pre-filtering.

    Returns ``0.0`` when the market pays no rewards, the spread parameter
    is non-positive, the target sits outside the reward band, or the
    target price is implausible (``<= 0``).
    """
    if (
        daily_reward_usd <= 0.0
        or max_spread_pct <= 0.0
        or target_price <= 0.0
        or midpoint <= 0.0
        or size_usd <= 0.0
    ):
        return 0.0

    v = max_spread_pct / 100.0
    # Reward band is ±v around mid; outside the band S clamps to 0 anyway
    # but we skip early so a degenerate target returns a clean 0.
    if abs(target_price - midpoint) > v:
        return 0.0

    # My contribution: ``size_usd / price`` shares at this level.
    added_shares = size_usd / target_price

    # Existing size at the target level (so we share the level's reward
    # pool rather than dominating it).
    existing_target_size = 0.0
    for price, size in book_levels:
        if abs(price - target_price) < 1e-9:
            existing_target_size = max(0.0, size)
            break

    target_total_size = existing_target_size + added_shares
    s_target = abs(target_price - midpoint)
    shape_target = ((v - s_target) / v) ** 2
    q_target = shape_target * target_total_size

    # Sum Q across ALL eligible book levels on the side. Levels equal to
    # the target are replaced by our augmented size; other levels use
    # their existing size unchanged. Zero-size levels contribute zero.
    q_total = q_target
    for price, size in book_levels:
        if size <= 0.0:
            continue
        distance = abs(price - midpoint)
        if distance > v:
            continue
        if abs(price - target_price) < 1e-9:
            continue  # target already counted via q_target above
        shape = ((v - distance) / v) ** 2
        q_total += shape * size

    if q_total <= 0.0:
        return 0.0

    # Half the daily pool per side; our fraction of Q claims that slice;
    # normalise by shares-at-level to isolate OUR contribution.
    side_pool = daily_reward_usd / 2.0
    level_payout = (q_target / q_total) * side_pool
    my_share_of_level = added_shares / target_total_size
    return level_payout * my_share_of_level


def estimate_reward_per_100(
    target_price: float,
    midpoint: float,
    book_levels: list[tuple[float, float]],
    max_spread_pct: float,
    daily_reward_usd: float,
) -> float:
    """Compatibility wrapper around :func:`estimate_reward_for_size` at
    the historical $100 quote size. Existing callers (daemon journal
    instrumentation, tests) keep working unchanged.
    """
    return estimate_reward_for_size(
        target_price=target_price,
        midpoint=midpoint,
        book_levels=book_levels,
        max_spread_pct=max_spread_pct,
        daily_reward_usd=daily_reward_usd,
        size_usd=100.0,
    )
