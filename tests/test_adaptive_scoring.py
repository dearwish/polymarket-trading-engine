"""Unit tests for AdaptiveScorer (phase 2 adaptive-regime branch).

The wrapper is deliberately thin — these tests lock in the
regime → side-decision contract so future regime logic changes
don't silently flip the scorer's behaviour.
"""
from __future__ import annotations

from pathlib import Path

from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.engine.adaptive_scoring import AdaptiveScorer
from polymarket_trading_engine.engine.quant_scoring import QuantScoringEngine
from polymarket_trading_engine.types import EvidencePacket, SuggestedSide


def _settings(tmp_path: Path) -> Settings:
    """Fade-scorer settings loose enough for the underlying engine to
    produce a non-ABSTAIN side in the tests that need one.
    """
    return Settings(
        db_path=tmp_path / "agent.db",
        heartbeat_path=tmp_path / "heartbeat.json",
        min_edge=0.01,
        min_confidence=0.0,
        quant_trend_filter_enabled=False,
        quant_ofi_gate_enabled=False,
        quant_vol_regime_enabled=False,
        quant_min_entry_price=0.0,
    )


def _ranging_packet(**overrides) -> EvidencePacket:
    """Packet with drift and ask prices that make the fade scorer pick
    YES with positive edge, and HTF features that read as RANGING so the
    adaptive scorer lets the pick through.
    """
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
        bid_yes=0.48,
        ask_yes=0.50,
        bid_no=0.50,
        ask_no=0.52,
        imbalance_top5_yes=0.0,
        btc_log_return_since_candle_open=0.002,
        realized_vol_30m=0.002,
        # Both HTF returns present but small → RANGING per default thresholds.
        btc_log_return_1h=0.0005,
        btc_log_return_4h=0.0005,
        time_elapsed_in_candle_s=300,
    )
    defaults.update(overrides)
    return EvidencePacket(**defaults)


def test_adaptive_delegates_in_ranging(tmp_path: Path) -> None:
    """In RANGING regime the adaptive scorer must return exactly what the
    underlying fade scorer would, no rewriting.
    """
    fade = QuantScoringEngine(_settings(tmp_path))
    adaptive = AdaptiveScorer(fade)
    packet = _ranging_packet()
    fade_result = fade.score_market(packet)
    assert fade_result.suggested_side != SuggestedSide.ABSTAIN, "setup: fade must trade"
    adaptive_result = adaptive.score_market(packet)
    assert adaptive_result.suggested_side == fade_result.suggested_side
    assert adaptive_result.edge == fade_result.edge
    assert adaptive_result.fair_probability == fade_result.fair_probability


def test_adaptive_abstains_on_high_vol(tmp_path: Path) -> None:
    """HIGH_VOL regime forces ABSTAIN even with a positive fade edge."""
    fade = QuantScoringEngine(_settings(tmp_path))
    adaptive = AdaptiveScorer(fade)
    packet = _ranging_packet(realized_vol_30m=0.010)  # above vol_high=0.005
    # Sanity check the fade scorer would have traded here.
    assert fade.score_market(packet).suggested_side != SuggestedSide.ABSTAIN
    result = adaptive.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert result.edge == 0.0
    assert result.confidence == 0.0
    assert any("Regime HIGH_VOL" in r for r in result.reasons_to_abstain)


def test_adaptive_delegates_to_fade_in_trending_up(tmp_path: Path) -> None:
    """TRENDING_UP used to route to follow-with-maker (take YES), but the
    2026-04-23 soak showed 15-min BTC candles mean-revert — 5.9% hit on
    17 TRENDING_UP entries taking YES, net −$6. Adaptive now delegates
    to fade in trending regimes so its taker path handles the entry
    (fade had 50% hit in the same trending bucket).
    """
    fade = QuantScoringEngine(_settings(tmp_path))
    adaptive = AdaptiveScorer(fade)
    packet = _ranging_packet(
        btc_log_return_1h=0.005,
        btc_log_return_4h=0.008,
        realized_vol_30m=0.002,
    )
    fade_result = fade.score_market(packet)
    adaptive_result = adaptive.score_market(packet)
    assert adaptive_result.suggested_side == fade_result.suggested_side
    assert adaptive_result.edge == fade_result.edge
    assert adaptive_result.raw_model_output == fade_result.raw_model_output
    # Must NOT use the maker-routing tag anymore — the daemon's
    # paper-maker lifecycle should never fire from adaptive until we
    # re-architect the follow branch.
    from polymarket_trading_engine.engine.adaptive_scoring import ADAPTIVE_FOLLOW_MAKER_TAG
    assert adaptive_result.raw_model_output != ADAPTIVE_FOLLOW_MAKER_TAG


def test_adaptive_delegates_to_fade_in_trending_down(tmp_path: Path) -> None:
    """Mirror of the TRENDING_UP case: 233 TRENDING_DOWN entries taking
    NO hit 16.7% (−$67). Delegate to fade instead.
    """
    fade = QuantScoringEngine(_settings(tmp_path))
    adaptive = AdaptiveScorer(fade)
    packet = _ranging_packet(
        btc_log_return_1h=-0.005,
        btc_log_return_4h=-0.008,
        realized_vol_30m=0.002,
    )
    fade_result = fade.score_market(packet)
    adaptive_result = adaptive.score_market(packet)
    assert adaptive_result.suggested_side == fade_result.suggested_side
    assert adaptive_result.edge == fade_result.edge
    from polymarket_trading_engine.engine.adaptive_scoring import ADAPTIVE_FOLLOW_MAKER_TAG
    assert adaptive_result.raw_model_output != ADAPTIVE_FOLLOW_MAKER_TAG


def test_adaptive_abstains_on_unknown(tmp_path: Path) -> None:
    """HTF buffer cold-start (both returns 0) → UNKNOWN → ABSTAIN."""
    fade = QuantScoringEngine(_settings(tmp_path))
    adaptive = AdaptiveScorer(fade)
    packet = _ranging_packet(btc_log_return_1h=0.0, btc_log_return_4h=0.0)
    result = adaptive.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("UNKNOWN" in r for r in result.reasons_to_abstain)


def test_adaptive_preserves_fair_probability_across_regimes(tmp_path: Path) -> None:
    """The wrapper must not rewrite fair_probability — downstream Brier
    and calibration analysis must still be apples-to-apples with the
    fade scorer's output.
    """
    fade = QuantScoringEngine(_settings(tmp_path))
    adaptive = AdaptiveScorer(fade)
    packet = _ranging_packet(
        btc_log_return_1h=0.005,
        btc_log_return_4h=0.008,
    )
    fade_result = fade.score_market(packet)
    adaptive_result = adaptive.score_market(packet)
    assert adaptive_result.fair_probability == fade_result.fair_probability
    assert adaptive_result.edge_yes == fade_result.edge_yes
    assert adaptive_result.edge_no == fade_result.edge_no


def test_adaptive_raw_model_output_identifies_branch(tmp_path: Path) -> None:
    """raw_model_output lets analyze_soak / journals tell which branch
    of the adaptive scorer fired. Two labels: the gated tag in hold-fire
    regimes, and the fade scorer's own label when passing through (both
    RANGING and now trending, since trending delegates to fade).
    """
    fade = QuantScoringEngine(_settings(tmp_path))
    adaptive = AdaptiveScorer(fade)
    # Trending → delegates to fade → passes fade's own tag.
    trend = _ranging_packet(btc_log_return_1h=0.005, btc_log_return_4h=0.008)
    assert adaptive.score_market(trend).raw_model_output == "quant-scoring"
    # HIGH_VOL → gated tag (still abstains).
    chop = _ranging_packet(realized_vol_30m=0.010)
    assert adaptive.score_market(chop).raw_model_output == "adaptive-regime-gated"
    # RANGING → fade scorer's output passes through unchanged.
    ranging = _ranging_packet()
    assert adaptive.score_market(ranging).raw_model_output == "quant-scoring"


def test_adaptive_abstains_on_pre_market_even_in_trending(tmp_path: Path) -> None:
    """Bug fix: a pre-market candle's ASK hasn't settled and our maker
    TTL could straddle the candle open, so follow-maker is meaningless
    there. The scorer must abstain regardless of HTF regime.
    """
    fade = QuantScoringEngine(_settings(tmp_path))
    adaptive = AdaptiveScorer(fade)
    # TRENDING_UP would normally fire follow-maker, but is_pre_market wins.
    packet = _ranging_packet(
        btc_log_return_1h=0.005,
        btc_log_return_4h=0.008,
        is_pre_market=True,
    )
    result = adaptive.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert result.raw_model_output == "adaptive-regime-gated"
    assert any("Pre-market" in r for r in result.reasons_to_abstain)
