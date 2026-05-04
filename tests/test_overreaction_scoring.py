"""Unit tests for :class:`OverreactionScorer` (adaptive_v2).

The scorer is a pure function of the packet; these tests lock in the
overreaction formula + gate contract so future tuning doesn't silently
flip the decision surface.
"""
from __future__ import annotations

from polymarket_trading_engine.engine.overreaction_scoring import (
    OVERREACTION_POST_ONLY_TAG,
    OVERREACTION_TAG,
    OverreactionScorer,
)
from polymarket_trading_engine.types import EvidencePacket, SuggestedSide


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
        external_price=70000.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=0.49,
        ask_yes=0.51,
        bid_no=0.49,
        ask_no=0.51,
        btc_log_return_5m=0.0,
        realized_vol_30m=0.003,
    )
    defaults.update(overrides)
    return EvidencePacket(**defaults)


def test_fades_upward_overreaction_by_betting_no() -> None:
    """PM mid jumped +4% (400 bps) while BTC only moved +0.1% (which at
    sensitivity=10 justifies a +1% mid move). Excess is +3%, above the
    2% threshold → bet NO, expecting the mid to mean-revert down.
    """
    scorer = OverreactionScorer(overreaction_threshold=0.02, sensitivity=10.0)
    packet = _packet(recent_price_change_bps=400.0, btc_log_return_5m=0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.NO
    assert result.raw_model_output == OVERREACTION_TAG
    # fair_probability should be BELOW current mid (we think mid will fall)
    assert result.fair_probability < packet.orderbook_midpoint
    # Edge should be |overreaction| − cost_floor > 0
    assert result.edge > 0.0
    assert result.edge_no == result.edge


def test_fades_downward_overreaction_by_betting_yes() -> None:
    """PM mid dropped −4% while BTC only moved −0.1% (justifies −1%).
    Excess is −3%, |excess|=3% > 2% threshold → bet YES (expect bounce).
    """
    scorer = OverreactionScorer(overreaction_threshold=0.02, sensitivity=10.0)
    packet = _packet(recent_price_change_bps=-400.0, btc_log_return_5m=-0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.YES
    assert result.fair_probability > packet.orderbook_midpoint
    assert result.edge_yes == result.edge
    assert result.edge_no == 0.0


def test_abstains_when_excess_below_threshold() -> None:
    """PM moved +1%, BTC moved +0.08% × sensitivity 10 → expected +0.8%.
    Excess is 0.2% — below the 2% threshold — so ABSTAIN even though
    technically the mid overshot.
    """
    scorer = OverreactionScorer(overreaction_threshold=0.02, sensitivity=10.0)
    packet = _packet(recent_price_change_bps=100.0, btc_log_return_5m=0.0008)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("threshold" in r for r in result.reasons_to_abstain)


def test_abstains_when_pm_move_matches_btc_justification() -> None:
    """PM moved exactly as BTC justifies (zero excess) — no signal, abstain.
    Sanity guard against firing on any motion.
    """
    scorer = OverreactionScorer(overreaction_threshold=0.02, sensitivity=10.0)
    # pm_move = 0.01 = 100 bps; btc_move = 0.001; expected = 0.01 → zero excess
    packet = _packet(recent_price_change_bps=100.0, btc_log_return_5m=0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN


def test_abstains_on_pre_market() -> None:
    scorer = OverreactionScorer()
    packet = _packet(
        recent_price_change_bps=400.0,
        btc_log_return_5m=0.001,
        is_pre_market=True,
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("pre-market" in r.lower() for r in result.reasons_to_abstain)


def test_abstains_when_no_data_yet() -> None:
    """Cold-start tick — both PM and BTC deltas are 0. The scorer must
    not fire (both being 0 is a missing-history sentinel, not a signal).
    """
    scorer = OverreactionScorer()
    packet = _packet(recent_price_change_bps=0.0, btc_log_return_5m=0.0)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN


def test_abstains_when_tte_too_short_for_reversion() -> None:
    """A genuine overreaction with only 30s TTE has no reversion window;
    we'd just catch the last minute's noise spike. Abstain.
    """
    scorer = OverreactionScorer(min_seconds_to_expiry=60)
    packet = _packet(
        recent_price_change_bps=400.0,
        btc_log_return_5m=0.001,
        seconds_to_expiry=30,
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("TTE" in r for r in result.reasons_to_abstain)


def test_abstains_when_excess_does_not_recover_cost_floor() -> None:
    """Excess just above threshold but below cost_floor means the edge
    is zero or negative after fees — abstain rather than trade to a loss.
    """
    # threshold 0.02, cost_floor 0.03 → need |excess| > 0.03 to get edge > 0.
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0, cost_floor=0.03
    )
    # Excess = 0.025 → above threshold (0.02) but below cost_floor (0.03).
    # pm=350 bps (0.035), btc_implied=100 bps (0.01) → excess=0.025.
    packet = _packet(recent_price_change_bps=350.0, btc_log_return_5m=0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("cost_floor" in r for r in result.reasons_to_abstain)


def test_fair_probability_clamps_to_valid_range() -> None:
    """Extreme overreaction at an already-extreme mid should clamp fair
    into [0.01, 0.99] rather than producing an invalid probability.
    """
    scorer = OverreactionScorer(overreaction_threshold=0.02, sensitivity=10.0)
    # Current mid 0.95, pm jumped +10% (already saturating) — fair for NO
    # bet would be 0.95 − 0.10 = 0.85, which is fine. Flip the other way:
    # mid 0.95, pm dropped −15%, btc only −0.1% → excess=−0.14, fair_yes =
    # 0.95 + 0.14 = 1.09 → must clamp to 0.99.
    packet = _packet(
        orderbook_midpoint=0.95,
        recent_price_change_bps=-1500.0,
        btc_log_return_5m=-0.001,
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.YES
    assert result.fair_probability <= 0.99


def test_raw_model_output_always_tagged() -> None:
    """Every branch (approved + abstain) carries OVERREACTION_TAG so the
    daemon's strategy routing and analyze_soak can attribute decisions
    to this scorer without a side-channel lookup.
    """
    scorer = OverreactionScorer()
    approved = scorer.score_market(
        _packet(recent_price_change_bps=400.0, btc_log_return_5m=0.001)
    )
    abstained = scorer.score_market(_packet())
    assert approved.raw_model_output == OVERREACTION_TAG
    assert abstained.raw_model_output == OVERREACTION_TAG


def test_btc_30s_takes_precedence_over_5m_when_both_present() -> None:
    """The scorer's BTC reference is the 30s window — matched to the
    Polymarket mid-change horizon. When both 30s and 5m are present, 30s
    wins. Reproduces the structural fix for the 2026-04-25 mkt 2068470
    falling-knife scenario where a 5m window read ~0% while spot was
    crashing right then.
    """
    # Construct: PM mid dumped -20% over 30s. The 5m BTC return looks
    # benign (-0.03%) but the 30s BTC return is -0.10% (real crash). At
    # sensitivity=10 the 30s window justifies a -1% mid move; PM
    # actually moved -20% → excess -19%. Capped by max_abs_edge below.
    scorer = OverreactionScorer(
        overreaction_threshold=0.02,
        sensitivity=10.0,
        cost_floor=0.005,
        max_abs_edge=0.0,  # disable ceiling so we can isolate the BTC-window choice
    )
    packet = _packet(
        recent_price_change_bps=-2000.0,
        btc_log_return_30s=-0.001,
        btc_log_return_5m=-0.0003,
    )
    result = scorer.score_market(packet)
    # With 30s as the reference: expected_pm_move = -0.001 * 10 = -0.01,
    #   excess = -0.20 − (-0.01) = -0.19; |excess|=0.19 → edge ≈ 0.185
    # With 5m as the reference (broken): expected = -0.003,
    #   excess = -0.20 − (-0.003) = -0.197; |excess|≈0.197 → edge ≈ 0.192
    # Either way the side is YES; the precedence is asserted via the edge
    # value, which only matches the 30s computation.
    assert result.suggested_side == SuggestedSide.YES
    assert abs(result.edge - 0.185) < 1e-3, f"expected ~0.185 (30s), got {result.edge:.4f}"


def test_max_abs_edge_ceiling_abstains_on_suspiciously_large_excess() -> None:
    """Reproduce the mkt 2068470 trade #3 entry: PM was crashing in
    free-fall while BTC's 30s window hadn't fully captured the move yet.
    The scorer would compute a +20% edge — the empirically worst-PnL
    bucket. The ceiling forces ABSTAIN.
    """
    scorer = OverreactionScorer(
        overreaction_threshold=0.02,
        sensitivity=10.0,
        cost_floor=0.005,
        max_abs_edge=0.30,
    )
    packet = _packet(
        recent_price_change_bps=-3500.0,  # PM dumped -35%
        btc_log_return_30s=-0.0005,        # BTC barely moved on the 30s window
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("ceiling" in r for r in result.reasons_to_abstain)


def test_max_abs_edge_zero_disables_ceiling() -> None:
    """Operators can opt out of the ceiling entirely with max_abs_edge=0.0."""
    scorer = OverreactionScorer(
        overreaction_threshold=0.02,
        sensitivity=10.0,
        cost_floor=0.005,
        max_abs_edge=0.0,
    )
    packet = _packet(recent_price_change_bps=-3500.0, btc_log_return_30s=-0.0005)
    result = scorer.score_market(packet)
    # Without the ceiling the trade goes through despite the suspiciously
    # large excess.
    assert result.suggested_side == SuggestedSide.YES
    assert result.edge > 0.30


def test_post_only_stamps_post_only_tag_on_approved_assessments() -> None:
    """When ``post_only=True``, an APPROVED overreaction assessment carries
    the maker-routing tag so the daemon parks a resting limit instead of
    crossing the spread.
    """
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0, post_only=True,
    )
    packet = _packet(recent_price_change_bps=400.0, btc_log_return_5m=0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.NO
    assert result.raw_model_output == OVERREACTION_POST_ONLY_TAG


def test_post_only_does_not_affect_abstain_assessments() -> None:
    """ABSTAIN paths use the legacy OVERREACTION_TAG regardless of
    post_only — there's nothing to route through the maker lifecycle.
    """
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0, post_only=True,
    )
    # Sub-threshold excess (1% < 2%) → ABSTAIN.
    packet = _packet(recent_price_change_bps=100.0, btc_log_return_5m=0.0)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert result.raw_model_output == OVERREACTION_TAG


def test_post_only_default_false_preserves_legacy_taker_tag() -> None:
    """Default constructor preserves the original OVERREACTION_TAG so
    existing callers don't accidentally flip into the maker lifecycle.
    """
    scorer = OverreactionScorer(overreaction_threshold=0.02, sensitivity=10.0)
    packet = _packet(recent_price_change_bps=400.0, btc_log_return_5m=0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.NO
    assert result.raw_model_output == OVERREACTION_TAG


def test_ofi_gate_blocks_when_flow_opposes_side() -> None:
    """Strong informed sell-flow (negative signed_flow_5s) opposes a YES
    bet — gate must abstain. Mirrors QuantScoringEngine's OFI logic so
    both scorers respect the same adverse-selection floor.
    """
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0,
        ofi_gate_enabled=True, ofi_gate_min_abs_flow=25.0,
    )
    # Mid dropped −4% with BTC flat → bet YES (expect bounce).
    # But flow=−50 (heavy selling) opposes YES → abstain.
    packet = _packet(
        recent_price_change_bps=-400.0, btc_log_return_5m=0.0,
        signed_flow_5s=-50.0,
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("OFI gate" in r for r in result.reasons_to_abstain)


def test_ofi_gate_allows_flow_aligned_with_side() -> None:
    """Flow matching our side direction is NOT blocked — the gate only
    fires when flow OPPOSES our trade.
    """
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0,
        ofi_gate_enabled=True, ofi_gate_min_abs_flow=25.0,
    )
    # Mid dropped → bet YES; flow=+50 (buying) aligns with YES → allow.
    packet = _packet(
        recent_price_change_bps=-400.0, btc_log_return_5m=0.0,
        signed_flow_5s=50.0,
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.YES
    assert not any("OFI gate" in r for r in result.reasons_to_abstain)


def test_ofi_gate_passes_when_flow_below_threshold() -> None:
    """Below-threshold flow is treated as noise; gate stays silent."""
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0,
        ofi_gate_enabled=True, ofi_gate_min_abs_flow=25.0,
    )
    # Same setup but flow=−10 (under 25 threshold) → no abstain.
    packet = _packet(
        recent_price_change_bps=-400.0, btc_log_return_5m=0.0,
        signed_flow_5s=-10.0,
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.YES
    assert not any("OFI gate" in r for r in result.reasons_to_abstain)


def test_ofi_gate_disabled_by_default() -> None:
    """Default constructor has the OFI gate off — opt-in via daemon wiring."""
    scorer = OverreactionScorer(overreaction_threshold=0.02, sensitivity=10.0)
    packet = _packet(
        recent_price_change_bps=-400.0, btc_log_return_5m=0.0,
        signed_flow_5s=-100.0,
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.YES


def test_invert_flips_upward_overreaction_to_yes() -> None:
    """With ``invert=True``, an upward overshoot bets YES (continuation)
    instead of NO (reversion). Mirrors quant_invert_drift on fade after
    the 2026-04-30 soak showed the reversion thesis was the wrong sign.
    """
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0, invert=True,
    )
    # Same packet as test_fades_upward_overreaction_by_betting_no — only
    # the inversion flag differs.
    packet = _packet(recent_price_change_bps=400.0, btc_log_return_5m=0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.YES
    assert result.fair_probability > packet.orderbook_midpoint
    assert result.edge_yes == result.edge
    assert result.edge_no == 0.0


def test_invert_flips_downward_overreaction_to_no() -> None:
    """Downward overshoot bets NO (continuation down) when inverted."""
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0, invert=True,
    )
    packet = _packet(recent_price_change_bps=-400.0, btc_log_return_5m=-0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.NO
    assert result.fair_probability < packet.orderbook_midpoint
    assert result.edge_no == result.edge
    assert result.edge_yes == 0.0


def test_invert_default_false_preserves_reversion_behavior() -> None:
    """Default constructor remains the original reversion thesis so
    existing callers don't accidentally flip into momentum."""
    scorer = OverreactionScorer(overreaction_threshold=0.02, sensitivity=10.0)
    # Upward overshoot → bet NO under the default (reversion).
    packet = _packet(recent_price_change_bps=400.0, btc_log_return_5m=0.001)
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.NO


def test_invert_respects_ofi_gate_with_continuation_side() -> None:
    """OFI gate still fires when flow opposes the inverted side.
    Inverted: upward overshoot → bet YES; if flow is heavy SELL (−50),
    the gate must abstain just as it would for a non-inverted YES.
    """
    scorer = OverreactionScorer(
        overreaction_threshold=0.02, sensitivity=10.0, invert=True,
        ofi_gate_enabled=True, ofi_gate_min_abs_flow=25.0,
    )
    packet = _packet(
        recent_price_change_bps=400.0, btc_log_return_5m=0.001,
        signed_flow_5s=-50.0,
    )
    result = scorer.score_market(packet)
    assert result.suggested_side == SuggestedSide.ABSTAIN
    assert any("OFI gate" in r for r in result.reasons_to_abstain)
