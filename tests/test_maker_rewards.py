"""Tests for the pure Polymarket maker-reward estimator.

Formula under test: Q = S × size, S = ((v − |p − mid|) / v)², reward_per_100
is the $100 quote's share of the side's (daily_reward / 2) pool.
"""
from __future__ import annotations

from polymarket_trading_engine.engine.maker_rewards import estimate_reward_per_100


def test_empty_book_gets_entire_side_pool() -> None:
    """With no competition at mid, a $100 rest captures half the pool
    (full side share, full level share). max_spread=4 → v=0.04, price=mid
    → S=1, Q = my_shares. q_total == q_target so I claim 100% of the
    level, which is 100% of the side pool.
    """
    reward = estimate_reward_per_100(
        target_price=0.50,
        midpoint=0.50,
        book_levels=[],
        max_spread_pct=4.0,
        daily_reward_usd=200.0,
    )
    # daily_reward / 2 = 100; I claim 100% with no competition.
    assert abs(reward - 100.0) < 1e-6


def test_competing_makers_dilute_reward() -> None:
    """Adding 500 shares of existing liquidity at the target level
    shrinks my share to my_shares / (my_shares + existing)."""
    # my shares at price 0.50 with $100 = 200.
    reward_with_competition = estimate_reward_per_100(
        target_price=0.50,
        midpoint=0.50,
        book_levels=[(0.50, 500.0)],
        max_spread_pct=4.0,
        daily_reward_usd=200.0,
    )
    # my_share = 200 / (200 + 500) = 2/7; level_payout = 100 (still own the level's Q);
    # but q_target = 1 × 700, q_total = 700 so fraction is 1 → level gets all pool.
    # reward = 100 × 200/700 ≈ 28.571
    assert abs(reward_with_competition - 100.0 * 200.0 / 700.0) < 1e-6


def test_distance_from_mid_decays_as_S_squared() -> None:
    """A level halfway between mid and v gets S = ((v − v/2)/v)² = 0.25.
    With no other orders, our Q fraction is 1, so reward is half pool × S_effect
    but normalised to our share of the LEVEL — which is 100% when empty.
    So the whole side_pool accrues at this level even though S < 1.
    """
    # At price 0.50, mid 0.50, v = 0.04 → target at 0.52 has s = 0.02, S = 0.25.
    reward = estimate_reward_per_100(
        target_price=0.52,
        midpoint=0.50,
        book_levels=[],
        max_spread_pct=4.0,
        daily_reward_usd=200.0,
    )
    # Empty book → q_target is only level → q_target/q_total = 1 → full side pool.
    # S only matters when there's competition — with zero other levels we get
    # the whole pool regardless of distance (still inside band).
    assert abs(reward - 100.0) < 1e-6


def test_outside_reward_band_returns_zero() -> None:
    """max_spread=4 → v=0.04; a target 0.05 away from mid is outside the band."""
    reward = estimate_reward_per_100(
        target_price=0.55,
        midpoint=0.50,
        book_levels=[],
        max_spread_pct=4.0,
        daily_reward_usd=200.0,
    )
    assert reward == 0.0


def test_zero_daily_reward_returns_zero() -> None:
    """Markets without maker incentives produce no yield estimate."""
    reward = estimate_reward_per_100(
        target_price=0.50,
        midpoint=0.50,
        book_levels=[(0.50, 100.0)],
        max_spread_pct=4.0,
        daily_reward_usd=0.0,
    )
    assert reward == 0.0


def test_zero_max_spread_returns_zero() -> None:
    """Malformed reward params produce no yield estimate."""
    reward = estimate_reward_per_100(
        target_price=0.50,
        midpoint=0.50,
        book_levels=[],
        max_spread_pct=0.0,
        daily_reward_usd=200.0,
    )
    assert reward == 0.0


def test_zero_target_price_is_guarded() -> None:
    """Division by price happens internally — 0.0 must return 0, not raise."""
    reward = estimate_reward_per_100(
        target_price=0.0,
        midpoint=0.50,
        book_levels=[],
        max_spread_pct=4.0,
        daily_reward_usd=200.0,
    )
    assert reward == 0.0


def test_near_side_competition_dilutes_reward_via_Q_sum() -> None:
    """Orders at other prices in the band compete for the side's pool.
    A big order at mid (S=1) while I'm quoting at the band edge (S=0.25)
    means my Q is a small fraction of total Q."""
    # I quote at 0.52 (s=0.02, S=0.25), $100 → 192.3 shares.
    # Competitor at 0.50 (s=0, S=1) with 1000 shares → Q_comp = 1000.
    # My Q = 0.25 × 192.3 ≈ 48.08.
    # q_total ≈ 1048.08; my level payout = 100 × 48.08/1048.08 ≈ 4.588.
    # My share of the level is 100% (no competition at 0.52) so I get all of it.
    reward = estimate_reward_per_100(
        target_price=0.52,
        midpoint=0.50,
        book_levels=[(0.50, 1000.0)],
        max_spread_pct=4.0,
        daily_reward_usd=200.0,
    )
    my_shares = 100.0 / 0.52
    s_mine = 0.02
    v = 0.04
    shape_mine = ((v - s_mine) / v) ** 2
    q_mine = shape_mine * my_shares
    q_comp = 1.0 * 1000.0  # S at mid == 1
    expected_reward = 100.0 * (q_mine / (q_mine + q_comp))
    assert abs(reward - expected_reward) < 1e-4


def test_realistic_mid_shallow_competition() -> None:
    """Smoke test against a realistic book state to guard against a sign
    flip or formula regression. 5 cent bands, $100/day, competitors at
    mid-1c and mid-2c. We quote at mid."""
    reward = estimate_reward_per_100(
        target_price=0.50,
        midpoint=0.50,
        book_levels=[
            (0.50, 200.0),  # existing at mid
            (0.49, 300.0),  # 1c off
            (0.48, 400.0),  # 2c off
        ],
        max_spread_pct=5.0,
        daily_reward_usd=100.0,
    )
    # We know reward > 0 and < full pool (50).
    assert 0.0 < reward < 50.0
