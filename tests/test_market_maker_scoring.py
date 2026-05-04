"""Gate-by-gate tests for :class:`MarketMakerScorer`.

The scorer only decides "should the daemon quote this market this tick?".
Every gate failure must produce an ABSTAIN with a logged reason; every
pass must produce an APPROVED carrying the strategy tag so the daemon
can dispatch into the MM lifecycle handler.
"""
from __future__ import annotations

from polymarket_trading_engine.engine.market_maker.scorer import (
    MARKET_MAKER_STRATEGY_TAG,
    MarketMakerScorer,
)
from polymarket_trading_engine.types import EvidencePacket, SuggestedSide


def _packet(**overrides) -> EvidencePacket:
    """Sensible-defaults packet that PASSES every gate. Tests override
    just the field they're stressing so failure modes read cleanly.
    """
    defaults = dict(
        market_id="m",
        question="",
        resolution_criteria="",
        market_probability=0.55,
        orderbook_midpoint=0.55,
        spread=0.04,
        depth_usd=1000.0,
        seconds_to_expiry=300,
        external_price=0.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=0.53,
        ask_yes=0.57,
        bid_no=0.43,
        ask_no=0.47,
    )
    defaults.update(overrides)
    return EvidencePacket(**defaults)


def test_approves_with_two_sided_book_and_healthy_spread() -> None:
    scorer = MarketMakerScorer()
    result = scorer.score_market(_packet())
    assert result.suggested_side != SuggestedSide.ABSTAIN
    assert result.raw_model_output == MARKET_MAKER_STRATEGY_TAG


def test_does_not_abstain_on_pre_market_flag() -> None:
    """``is_pre_market`` is BTC-candle-specific (``tte > family_window_seconds``).
    For sports / politics markets in the MM universe with multi-day TTEs
    the flag fires unconditionally, so the MM scorer must ignore it and
    rely on the two-sided-book + spread gates instead.
    """
    scorer = MarketMakerScorer()
    # Pre-market flag set, but the book is healthy two-sided — MM should
    # still approve and quote.
    result = scorer.score_market(_packet(is_pre_market=True))
    assert result.suggested_side != SuggestedSide.ABSTAIN
    assert result.raw_model_output == MARKET_MAKER_STRATEGY_TAG


def test_abstains_when_tte_below_min() -> None:
    scorer = MarketMakerScorer(min_tte_seconds=120)
    result = scorer.score_market(_packet(seconds_to_expiry=60))
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("TTE" in r for r in result.reasons_to_abstain)


def test_abstains_when_book_one_sided() -> None:
    scorer = MarketMakerScorer()
    # Missing ask.
    result = scorer.score_market(_packet(ask_yes=0.0))
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("two-sided" in r for r in result.reasons_to_abstain)


def test_abstains_when_book_crossed() -> None:
    scorer = MarketMakerScorer()
    result = scorer.score_market(_packet(bid_yes=0.60, ask_yes=0.55))
    assert result.suggested_side == SuggestedSide.ABSTAIN


def test_abstains_when_spread_below_min() -> None:
    """A market spread tighter than ``min_market_spread`` means we'd
    rest INSIDE the existing spread — the strategy abstains because
    there's nothing to capture.
    """
    scorer = MarketMakerScorer(min_market_spread=0.03)
    result = scorer.score_market(_packet(bid_yes=0.55, ask_yes=0.56))
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("min" in r for r in result.reasons_to_abstain)


def test_abstains_when_spread_above_max() -> None:
    """Wide spreads on a binary market signal toxic flow — skip."""
    scorer = MarketMakerScorer(max_market_spread=0.10)
    result = scorer.score_market(_packet(bid_yes=0.30, ask_yes=0.50))
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("max" in r or "toxic" in r for r in result.reasons_to_abstain)


def test_max_market_spread_zero_disables_upper_gate() -> None:
    """Operator can disable the toxic-flow gate by setting max=0."""
    scorer = MarketMakerScorer(max_market_spread=0.0)
    result = scorer.score_market(_packet(bid_yes=0.30, ask_yes=0.70))
    assert result.suggested_side != SuggestedSide.ABSTAIN


def test_strategy_tag_consistent_across_approve_and_abstain() -> None:
    """Both branches must carry the same tag so the daemon can route on
    raw_model_output even on abstaining ticks (used in the daemon_tick
    journal payload).
    """
    scorer = MarketMakerScorer()
    approved = scorer.score_market(_packet())
    abstained = scorer.score_market(_packet(is_pre_market=True))
    assert approved.raw_model_output == MARKET_MAKER_STRATEGY_TAG
    assert abstained.raw_model_output == MARKET_MAKER_STRATEGY_TAG
