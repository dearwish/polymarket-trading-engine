"""Unit tests for :class:`PennyScorer`.

The scorer is a pure function of the packet. These tests lock in the
entry contract (threshold + TTE gate + pre-market abstain + side
selection) so future penny-strategy tweaks don't silently flip the
decision surface.
"""
from __future__ import annotations

from polymarket_ai_agent.engine.penny_scoring import PENNY_STRATEGY_TAG, PennyScorer
from polymarket_ai_agent.types import EvidencePacket, SuggestedSide


def _packet(**overrides) -> EvidencePacket:
    defaults = dict(
        market_id="m",
        question="",
        resolution_criteria="",
        market_probability=0.5,
        orderbook_midpoint=0.5,
        spread=0.02,
        depth_usd=500.0,
        seconds_to_expiry=600,
        external_price=0.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=0.97,
        ask_yes=0.98,
        bid_no=0.01,
        ask_no=0.02,  # penny ask on NO
    )
    defaults.update(overrides)
    return EvidencePacket(**defaults)


def test_enters_no_side_when_ask_no_below_threshold() -> None:
    scorer = PennyScorer(entry_thresh=0.03, min_entry_tte_seconds=300)
    result = scorer.score_market(_packet(ask_no=0.02, seconds_to_expiry=600))
    assert result.suggested_side == SuggestedSide.NO
    assert result.raw_model_output == PENNY_STRATEGY_TAG
    assert result.edge > 0.0
    assert result.edge_no == result.edge
    assert result.edge_yes == 0.0


def test_enters_yes_side_when_ask_yes_below_threshold() -> None:
    scorer = PennyScorer(entry_thresh=0.03, min_entry_tte_seconds=300)
    # Flip: YES becomes the cheap side.
    result = scorer.score_market(_packet(ask_yes=0.02, ask_no=0.98, seconds_to_expiry=600))
    assert result.suggested_side == SuggestedSide.YES
    assert result.edge_yes == result.edge
    assert result.edge_no == 0.0


def test_abstains_when_no_side_below_threshold() -> None:
    """Both asks above threshold — no penny setup, must abstain."""
    scorer = PennyScorer(entry_thresh=0.03, min_entry_tte_seconds=300)
    result = scorer.score_market(_packet(ask_yes=0.50, ask_no=0.52, seconds_to_expiry=600))
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("no side at" in r for r in result.reasons_to_abstain)


def test_abstains_when_tte_below_minimum() -> None:
    """Entering with <5min remaining is the terminal-cliff trap that the
    unfiltered backtest ate (−78% ROI). The TTE gate has to veto this.
    """
    scorer = PennyScorer(entry_thresh=0.03, min_entry_tte_seconds=300)
    result = scorer.score_market(_packet(ask_no=0.02, seconds_to_expiry=60))
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("TTE" in r for r in result.reasons_to_abstain)


def test_abstains_when_pre_market() -> None:
    """Pre-market candles have stale books — the penny thesis relies on
    a live book giving us a bounce window, neither of which holds here.
    """
    scorer = PennyScorer(entry_thresh=0.03, min_entry_tte_seconds=300)
    result = scorer.score_market(
        _packet(ask_no=0.02, seconds_to_expiry=600, is_pre_market=True)
    )
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("pre-market" in r.lower() for r in result.reasons_to_abstain)


def test_zero_ask_is_not_a_penny_setup() -> None:
    """A 0.0 ask is a missing-book sentinel, not a free trade. The
    scorer must guard against it explicitly.
    """
    scorer = PennyScorer(entry_thresh=0.03, min_entry_tte_seconds=300)
    result = scorer.score_market(_packet(ask_no=0.0, ask_yes=0.0, seconds_to_expiry=600))
    assert result.suggested_side == SuggestedSide.ABSTAIN


def test_picks_no_side_when_both_below_threshold() -> None:
    """Edge case: both sides below threshold (rare — implies an arbitrage
    or stale book). Preference is NO first to match the scorer's check
    order; the test locks this in so future refactors don't silently
    reorder side priority.
    """
    scorer = PennyScorer(entry_thresh=0.05, min_entry_tte_seconds=300)
    result = scorer.score_market(
        _packet(ask_yes=0.04, ask_no=0.03, seconds_to_expiry=600)
    )
    assert result.suggested_side == SuggestedSide.NO


def test_entry_threshold_is_inclusive() -> None:
    """An ask exactly at the threshold should qualify (``<=``, not ``<``)
    so one-tick markets at 3¢ don't fall out by floating-point luck.
    """
    scorer = PennyScorer(entry_thresh=0.03, min_entry_tte_seconds=300)
    result = scorer.score_market(_packet(ask_no=0.03, seconds_to_expiry=600))
    assert result.suggested_side == SuggestedSide.NO


def test_tte_gate_is_inclusive() -> None:
    """TTE exactly at the minimum should NOT abstain — the condition is
    ``< min``, so an entry at the floor is permitted.
    """
    scorer = PennyScorer(entry_thresh=0.03, min_entry_tte_seconds=300)
    result = scorer.score_market(_packet(ask_no=0.02, seconds_to_expiry=300))
    assert result.suggested_side == SuggestedSide.NO


def test_raw_model_output_tagged_for_daemon_routing() -> None:
    """Every output (approved OR abstain) carries the tag so analyze_soak
    and the daemon's routing branch can attribute the decision to this
    scorer without a side-channel lookup.
    """
    scorer = PennyScorer()
    approved = scorer.score_market(_packet(ask_no=0.02, seconds_to_expiry=600))
    abstained = scorer.score_market(_packet(ask_no=0.50, seconds_to_expiry=600))
    assert approved.raw_model_output == PENNY_STRATEGY_TAG
    assert abstained.raw_model_output == PENNY_STRATEGY_TAG
