from __future__ import annotations

import math
from pathlib import Path

from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.engine.quant_scoring import (
    FADE_POST_ONLY_TAG,
    QuantScoringEngine,
    _normal_cdf,
)
from polymarket_trading_engine.engine.research import ResearchEngine
from polymarket_trading_engine.types import EvidencePacket, SuggestedSide


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        openrouter_api_key="",
        polymarket_private_key="",
        polymarket_funder="",
        polymarket_signature_type=0,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
        # Pin all feature flags off so live .env values don't leak into tests.
        # Individual tests opt in by passing the relevant override.
        quant_invert_drift=False,
        quant_max_abs_edge=0.0,
        quant_trend_filter_enabled=False,
        quant_ofi_gate_enabled=False,
        quant_vol_regime_enabled=False,
        quant_trend_distressed_max_ask=0.0,
        quant_min_entry_price=0.0,
        quant_max_entry_price=0.0,
        min_candle_elapsed_seconds=0,
        max_candle_elapsed_seconds=0,
    )
    base.update(overrides)
    return Settings(**base)


def _packet(**overrides) -> EvidencePacket:
    defaults = dict(
        market_id="m1",
        question="Bitcoin up or down",
        resolution_criteria="-",
        market_probability=0.5,
        orderbook_midpoint=0.5,
        spread=0.02,
        depth_usd=500.0,
        seconds_to_expiry=900,
        external_price=70000.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=0.49,
        ask_yes=0.51,
        bid_no=0.49,
        ask_no=0.51,
        microprice_yes=0.5,
        imbalance_top5_yes=0.0,
        signed_flow_5s=0.0,
        btc_log_return_5m=0.0,
        btc_log_return_15m=0.0,
        realized_vol_30m=0.02,
    )
    defaults.update(overrides)
    return EvidencePacket(**defaults)


def test_normal_cdf_matches_textbook_values() -> None:
    assert abs(_normal_cdf(0.0) - 0.5) < 1e-9
    assert abs(_normal_cdf(1.0) - 0.8413447460685429) < 1e-6
    assert abs(_normal_cdf(-1.0) - 0.1586552539314571) < 1e-6


def test_fair_value_is_neutral_without_drift_or_imbalance(tmp_path: Path) -> None:
    engine = QuantScoringEngine(_settings(tmp_path))
    assessment = engine.score_market(_packet())
    assert abs(assessment.fair_probability - 0.5) < 1e-6
    # Ask is 0.51 on both sides: edges are -(0.01 + cost), both negative → abstain.
    assert assessment.suggested_side == SuggestedSide.ABSTAIN


def test_pre_market_ignores_rolling_returns(tmp_path: Path) -> None:
    """When the candle hasn't opened yet, rolling 5m/15m returns must not
    stand in for the candle-open drift. Prior behaviour: huge edges at
    discovery time, all dropped by the candle-window filter. New behaviour:
    fair = 0.5 + imbalance tilt only.
    """
    engine = QuantScoringEngine(_settings(tmp_path))
    packet = _packet(
        btc_log_return_15m=0.01,
        btc_log_return_5m=0.01,
        realized_vol_30m=0.02,
        seconds_to_expiry=1800,
        is_pre_market=True,
    )
    assessment = engine.score_market(packet)
    assert abs(assessment.fair_probability - 0.5) < 1e-6
    # Without an imbalance tilt, no positive edge → ABSTAIN.
    assert assessment.suggested_side == SuggestedSide.ABSTAIN


def test_positive_drift_biases_fair_above_half(tmp_path: Path) -> None:
    engine = QuantScoringEngine(_settings(tmp_path))
    packet = _packet(btc_log_return_15m=0.01, realized_vol_30m=0.02, seconds_to_expiry=1800)
    assessment = engine.score_market(packet)
    assert assessment.fair_probability > 0.5


def test_quant_invert_drift_flips_fair_around_half(tmp_path: Path) -> None:
    """When quant_invert_drift=True the scorer should return the mirror of
    its un-inverted fair_yes (around 0.5). Validates the mean-reversion
    test-flag does what it says."""
    # Same positive-drift packet evaluated with and without inversion.
    packet = _packet(btc_log_return_since_candle_open=0.01, realized_vol_30m=0.02, seconds_to_expiry=600)
    straight = QuantScoringEngine(_settings(tmp_path)).score_market(packet)
    inverted = QuantScoringEngine(_settings(tmp_path, quant_invert_drift=True)).score_market(packet)
    assert straight.fair_probability > 0.5
    assert inverted.fair_probability < 0.5
    # Mirror around 0.5 (within float noise and the 0.01/0.99 clamp).
    assert abs((straight.fair_probability + inverted.fair_probability) - 1.0) < 1e-6


def test_quant_max_abs_edge_forces_abstain_above_ceiling(tmp_path: Path) -> None:
    """When quant_max_abs_edge > 0 and the chosen-side edge exceeds it, the
    scorer must force ABSTAIN regardless of direction. Guards against the
    observed pattern where the highest-conviction picks were the worst picks.
    """
    settings = _settings(
        tmp_path,
        quant_slippage_baseline_bps=0.0,
        quant_slippage_spread_coef=0.0,
        fee_bps=0.0,
        quant_max_abs_edge=0.20,
    )
    engine = QuantScoringEngine(settings)
    # fair ~ 0.5, ask_yes 0.05 → edge_yes ≈ +0.45 > 0.20 ceiling → ABSTAIN.
    packet = _packet(ask_yes=0.05, ask_no=0.99)
    assessment = engine.score_market(packet)
    assert assessment.suggested_side == SuggestedSide.ABSTAIN
    assert assessment.confidence == 0.0
    assert any("exceeds |edge| ceiling" in r for r in assessment.reasons_to_abstain)
    # A moderate pick inside the ceiling is unaffected.
    moderate = engine.score_market(_packet(ask_yes=0.40, ask_no=0.55))
    assert moderate.suggested_side == SuggestedSide.YES


def test_fade_post_only_tags_non_abstain_assessments(tmp_path: Path) -> None:
    """fade_post_only=True must rewrite raw_model_output to the maker-routing
    sentinel for any non-ABSTAIN side, while leaving abstains unchanged. The
    daemon's strategy-tick router uses this string to push the assessment
    through the paper-maker lifecycle instead of the immediate taker fill.
    """
    settings = _settings(
        tmp_path,
        quant_slippage_baseline_bps=0.0,
        quant_slippage_spread_coef=0.0,
        fee_bps=0.0,
        fade_post_only=True,
    )
    engine = QuantScoringEngine(settings)
    # ask_yes 0.40 → fair 0.5 → edge_yes +0.10 → side YES, tag must flip.
    buy = engine.score_market(_packet(ask_yes=0.40, ask_no=0.55))
    assert buy.suggested_side == SuggestedSide.YES
    assert buy.raw_model_output == FADE_POST_ONLY_TAG
    # Symmetric ask=0.51 → no positive edge → ABSTAIN, tag stays default.
    abstain = engine.score_market(_packet())
    assert abstain.suggested_side == SuggestedSide.ABSTAIN
    assert abstain.raw_model_output == "quant-scoring"


def test_fade_post_only_off_keeps_default_tag(tmp_path: Path) -> None:
    """With the flag off the scorer must emit its normal raw_model_output
    even on a non-abstain side, so taker routing stays the default behaviour.
    """
    settings = _settings(
        tmp_path,
        quant_slippage_baseline_bps=0.0,
        quant_slippage_spread_coef=0.0,
        fee_bps=0.0,
    )
    engine = QuantScoringEngine(settings)
    buy = engine.score_market(_packet(ask_yes=0.40, ask_no=0.55))
    assert buy.suggested_side == SuggestedSide.YES
    assert buy.raw_model_output == "quant-scoring"


def test_candle_open_log_return_takes_precedence_over_rolling_windows(tmp_path: Path) -> None:
    """For "up or down" candle markets the scorer must use Δ_since_candle_open,
    not a rolling 5m/15m window. When the candle-open field is populated it
    should dominate the drift signal regardless of what the rolling fields say.
    """
    engine = QuantScoringEngine(_settings(tmp_path))
    # Rolling returns say bearish (-0.01) but we've observed +0.01 since the
    # market's own candle opened. The scorer must follow the candle-open signal
    # and bias fair_yes ABOVE 0.5.
    packet = _packet(
        btc_log_return_5m=-0.01,
        btc_log_return_15m=-0.01,
        btc_log_return_since_candle_open=0.01,
        realized_vol_30m=0.02,
        seconds_to_expiry=600,
    )
    assessment = engine.score_market(packet)
    assert assessment.fair_probability > 0.5, (
        f"fair_yes={assessment.fair_probability} — candle-open drift was positive, "
        "but scorer still biased fair below 0.5 (likely using rolling windows)."
    )

    # Mirror case: rolling says bullish, but we've fallen -0.01 since candle open
    # → fair_yes must be below 0.5.
    packet = _packet(
        btc_log_return_5m=0.01,
        btc_log_return_15m=0.01,
        btc_log_return_since_candle_open=-0.01,
        realized_vol_30m=0.02,
        seconds_to_expiry=600,
    )
    assessment = engine.score_market(packet)
    assert assessment.fair_probability < 0.5


def test_negative_drift_biases_fair_below_half(tmp_path: Path) -> None:
    engine = QuantScoringEngine(_settings(tmp_path))
    packet = _packet(btc_log_return_15m=-0.01, realized_vol_30m=0.02, seconds_to_expiry=1800)
    assessment = engine.score_market(packet)
    assert assessment.fair_probability < 0.5


def test_imbalance_tilts_fair_value(tmp_path: Path) -> None:
    engine = QuantScoringEngine(_settings(tmp_path, quant_imbalance_tilt=0.05))
    baseline = engine.score_market(_packet()).fair_probability
    bullish = engine.score_market(_packet(imbalance_top5_yes=0.8)).fair_probability
    assert bullish > baseline


def test_imbalance_tilt_keeps_natural_sign_when_drift_inverted(tmp_path: Path) -> None:
    """The imbalance tilt should always push fair_yes in the natural direction
    regardless of ``quant_invert_drift``. Soak attribution showed that
    inverting the tilt alongside drift was the single biggest loss source
    (against-pressure trades bled $0.51/trade on fade)."""
    settings = _settings(tmp_path, quant_invert_drift=True, quant_imbalance_tilt=0.10)
    engine = QuantScoringEngine(settings)
    # Strong YES book pressure should raise fair_yes even with drift inverted —
    # imbalance is a continuation signal, not a contrarian one.
    bullish = engine.score_market(_packet(imbalance_top5_yes=0.8)).fair_probability
    bearish = engine.score_market(_packet(imbalance_top5_yes=-0.8)).fair_probability
    assert bullish > bearish
    # And without drift, the inverted base is still 0.5 so the tilt direction
    # is purely the imbalance sign.
    neutral = engine.score_market(_packet(imbalance_top5_yes=0.0)).fair_probability
    assert abs(neutral - 0.5) < 1e-6
    assert bullish > neutral > bearish


def test_edge_subtracts_ask_and_costs(tmp_path: Path) -> None:
    settings = _settings(tmp_path, quant_slippage_baseline_bps=0.0, quant_slippage_spread_coef=0.0, fee_bps=0.0)
    engine = QuantScoringEngine(settings)
    packet = _packet(ask_yes=0.40, ask_no=0.55, bid_yes=0.38, bid_no=0.53)
    assessment = engine.score_market(packet)
    # fair_yes ~ 0.5 (no drift, no imbalance) → edge_yes = 0.5 - 0.40 = 0.10; edge_no = 0.5 - 0.55 = -0.05.
    assert abs(assessment.edge_yes - 0.10) < 1e-6
    assert abs(assessment.edge_no + 0.05) < 1e-6
    assert assessment.suggested_side == SuggestedSide.YES
    assert assessment.edge == assessment.edge_yes


def test_pick_side_chooses_no_when_no_side_has_higher_edge(tmp_path: Path) -> None:
    settings = _settings(tmp_path, quant_slippage_baseline_bps=0.0, quant_slippage_spread_coef=0.0, fee_bps=0.0)
    engine = QuantScoringEngine(settings)
    packet = _packet(ask_yes=0.55, ask_no=0.40)
    assessment = engine.score_market(packet)
    assert assessment.suggested_side == SuggestedSide.NO
    assert assessment.edge == assessment.edge_no


def test_confidence_scales_with_edge_and_is_capped(tmp_path: Path) -> None:
    engine = QuantScoringEngine(_settings(tmp_path))
    packet = _packet(ask_yes=0.05, ask_no=0.99)
    assessment = engine.score_market(packet)
    assert assessment.confidence > 0.5
    assert assessment.confidence <= 0.99


def test_expiry_risk_tiers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = QuantScoringEngine(settings)
    assert engine.score_market(_packet(seconds_to_expiry=10)).expiry_risk == "HIGH"
    assert engine.score_market(_packet(seconds_to_expiry=45)).expiry_risk == "MEDIUM"
    assert engine.score_market(_packet(seconds_to_expiry=600)).expiry_risk == "LOW"


def test_research_from_snapshot_populates_asks(market_snapshot, tmp_path: Path) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    assert packet.ask_yes > packet.bid_yes
    assert packet.ask_no > packet.bid_no
    # The REST path leaves BTC features at zero; scorer should fall back to default vol.
    engine = QuantScoringEngine(_settings(tmp_path))
    assessment = engine.score_market(packet)
    assert math.isfinite(assessment.fair_probability)
    assert math.isfinite(assessment.edge_yes)
    assert math.isfinite(assessment.edge_no)


def test_shadow_disabled_returns_none(tmp_path: Path) -> None:
    """score_shadow() must return None when quant_shadow_variant is empty."""
    engine = QuantScoringEngine(_settings(tmp_path, quant_shadow_variant=""))
    assert engine.score_shadow(_packet()) is None


def test_shadow_htf_tilt_nudges_fair_in_btc_trend_direction(tmp_path: Path) -> None:
    """htf_tilt variant should raise fair_yes when 1h BTC return is positive
    and lower it when negative, relative to the base scorer output."""
    engine = QuantScoringEngine(
        _settings(
            tmp_path,
            quant_shadow_variant="htf_tilt",
            quant_shadow_htf_tilt_strength=0.10,
            quant_shadow_session_bias_eu=0.0,
            quant_shadow_session_bias_us=0.0,
        )
    )
    base = engine.score_market(_packet()).fair_probability

    up = engine.score_shadow(_packet(btc_log_return_1h=0.005, btc_session="eu"))
    assert up is not None
    assert up.fair_probability > base
    assert up.raw_model_output == "quant-shadow-htf_tilt"

    down = engine.score_shadow(_packet(btc_log_return_1h=-0.005, btc_session="eu"))
    assert down is not None
    assert down.fair_probability < base


def test_shadow_session_bias_adds_independently_of_htf_tilt(tmp_path: Path) -> None:
    """Session bias stacks on top of HTF tilt; zero btc_log_return_1h means
    only the session bias shifts fair_yes."""
    engine = QuantScoringEngine(
        _settings(
            tmp_path,
            quant_shadow_variant="htf_tilt",
            quant_shadow_htf_tilt_strength=0.10,
            quant_shadow_session_bias_eu=0.05,
            quant_shadow_session_bias_us=0.0,
        )
    )
    no_session = engine.score_shadow(_packet(btc_log_return_1h=0.0, btc_session="off"))
    eu_session = engine.score_shadow(_packet(btc_log_return_1h=0.0, btc_session="eu"))
    assert no_session is not None and eu_session is not None
    assert abs(eu_session.fair_probability - (no_session.fair_probability + 0.05)) < 1e-6


def test_shadow_fade_invert_side_follows_live_abstain_and_flips_side(tmp_path: Path) -> None:
    """fade_invert_side mirrors live fair_yes (1 − p) and force-flips the
    chosen side, but inherits the live abstain decision so the chosen-tick
    Brier delta is computed on the same population."""
    engine = QuantScoringEngine(
        _settings(tmp_path, quant_shadow_variant="fade_invert_side")
    )
    # Picked side: drift signal big enough that live picks YES.
    packet = _packet(btc_log_return_since_candle_open=0.001)
    base = engine.score_market(packet)
    shadow = engine.score_shadow(packet, live=base)
    assert shadow is not None
    assert shadow.raw_model_output == "quant-shadow-fade_invert_side"
    assert abs(shadow.fair_probability - (1.0 - base.fair_probability)) < 1e-6
    if base.suggested_side is SuggestedSide.YES:
        assert shadow.suggested_side is SuggestedSide.NO
    elif base.suggested_side is SuggestedSide.NO:
        assert shadow.suggested_side is SuggestedSide.YES
    else:
        assert shadow.suggested_side is SuggestedSide.ABSTAIN


def test_shadow_fade_invert_side_inherits_live_abstain(tmp_path: Path) -> None:
    """When live abstains, shadow abstains — preserves the population for
    apples-to-apples Brier comparison."""
    # Slippage high enough that no edge survives on either side → ABSTAIN.
    engine = QuantScoringEngine(
        _settings(
            tmp_path,
            quant_shadow_variant="fade_invert_side",
            quant_slippage_baseline_bps=2000.0,
        )
    )
    packet = _packet()
    base = engine.score_market(packet)
    assert base.suggested_side is SuggestedSide.ABSTAIN
    shadow = engine.score_shadow(packet, live=base)
    assert shadow is not None
    assert shadow.suggested_side is SuggestedSide.ABSTAIN


def test_shadow_trades_still_use_base_assessment(tmp_path: Path) -> None:
    """score_shadow() returns a separate object; score_market() must be
    unaffected — same packet produces the same base assessment regardless of
    whether shadow is enabled."""
    settings_no_shadow = _settings(tmp_path, quant_shadow_variant="")
    settings_shadow = _settings(
        tmp_path,
        quant_shadow_variant="htf_tilt",
        quant_shadow_htf_tilt_strength=0.15,
    )
    packet = _packet(btc_log_return_1h=0.01, btc_session="us")
    base_no = QuantScoringEngine(settings_no_shadow).score_market(packet)
    base_yes = QuantScoringEngine(settings_shadow).score_market(packet)
    assert base_no.fair_probability == base_yes.fair_probability
    assert base_no.suggested_side == base_yes.suggested_side


def _regime_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings with all costs zeroed and regime gate enabled for isolated testing."""
    base = dict(
        quant_slippage_baseline_bps=0.0,
        quant_slippage_spread_coef=0.0,
        fee_bps=0.0,
        quant_trend_filter_enabled=True,
        quant_trend_filter_min_abs_return=0.003,
        quant_trend_opposed_strong_min_edge=0.15,
        quant_trend_opposed_weak_min_edge=0.06,
    )
    base.update(overrides)
    return _settings(tmp_path, **base)


def test_regime_gate_blocks_counter_trend_via_1h(tmp_path: Path) -> None:
    """1h trend signal blocks counter-trend trade when edge < weak threshold."""
    engine = QuantScoringEngine(_regime_settings(tmp_path))
    # Trend UP via 1h (+0.005), 4h flat. Drift=-0.01 → fair_yes≈0.40, fair_no≈0.60.
    # Edge_no = 0.60 - 0.56 = 0.04 < 0.06 required → blocked.
    packet = _packet(btc_log_return_1h=0.005, btc_log_return_4h=0.001,
                     btc_log_return_15m=-0.01, realized_vol_30m=0.02,
                     ask_yes=0.62, ask_no=0.56, seconds_to_expiry=1800)
    a = engine.score_market(packet)
    assert a.suggested_side == SuggestedSide.ABSTAIN
    assert a.confidence == 0.0
    assert any("Regime (1h" in r for r in a.reasons_to_abstain)


def test_regime_gate_allows_counter_trend_with_high_edge(tmp_path: Path) -> None:
    """Counter-trend trade passes when edge clears the elevated threshold."""
    engine = QuantScoringEngine(_regime_settings(tmp_path))
    # Trend UP via 1h (+0.005), 4h flat. Edge_no = 0.5 - 0.20 = 0.30 >> 0.06 required.
    packet = _packet(btc_log_return_1h=0.005, btc_log_return_4h=0.001,
                     btc_log_return_15m=-0.01, realized_vol_30m=0.02,
                     ask_yes=0.80, ask_no=0.20, seconds_to_expiry=1800)
    a = engine.score_market(packet)
    assert a.suggested_side == SuggestedSide.NO  # high edge clears the bar


def test_regime_gate_blocks_counter_trend_via_4h(tmp_path: Path) -> None:
    """4h takes priority: flat 1h but strong 4h blocks a counter-trend YES."""
    engine = QuantScoringEngine(_regime_settings(tmp_path))
    # 4h DOWN (-0.008), 1h flat. Edge_yes = 0.5 - 0.45 = 0.05 < 0.15 (strong threshold).
    packet = _packet(btc_log_return_4h=-0.008, btc_log_return_1h=-0.001,
                     btc_log_return_15m=0.01, realized_vol_30m=0.02,
                     ask_yes=0.45, ask_no=0.55, seconds_to_expiry=1800)
    a = engine.score_market(packet)
    assert a.suggested_side == SuggestedSide.ABSTAIN
    assert any("Regime (4h" in r for r in a.reasons_to_abstain)


def test_regime_gate_allows_with_trend_trade(tmp_path: Path) -> None:
    """Trade aligned with the 4h trend passes through at normal edge."""
    engine = QuantScoringEngine(_regime_settings(tmp_path))
    # Trend UP via 4h, scorer picks YES — no elevated threshold applies.
    packet = _packet(btc_log_return_4h=0.008, btc_log_return_1h=0.005,
                     btc_log_return_15m=0.01, realized_vol_30m=0.02,
                     ask_yes=0.40, ask_no=0.55, seconds_to_expiry=1800)
    a = engine.score_market(packet)
    assert a.suggested_side == SuggestedSide.YES
    assert a.confidence > 0.0


def test_regime_gate_disabled_ignores_trend(tmp_path: Path) -> None:
    """With filter disabled, a counter-trend pick passes through normally."""
    engine = QuantScoringEngine(_settings(
        tmp_path,
        quant_slippage_baseline_bps=0.0, quant_slippage_spread_coef=0.0, fee_bps=0.0,
        quant_trend_filter_enabled=False,
    ))
    packet = _packet(btc_log_return_4h=-0.008, btc_log_return_1h=0.005,
                     btc_log_return_15m=-0.01, realized_vol_30m=0.02,
                     ask_yes=0.55, ask_no=0.40, seconds_to_expiry=1800)
    assert engine.score_market(packet).suggested_side == SuggestedSide.NO


def test_regime_gate_inactive_in_ranging_market(tmp_path: Path) -> None:
    """When both |r4h| and |r1h| < threshold no trend gate applies."""
    engine = QuantScoringEngine(_regime_settings(tmp_path))
    packet = _packet(btc_log_return_4h=0.001, btc_log_return_1h=0.001,
                     btc_log_return_15m=-0.01, realized_vol_30m=0.02,
                     ask_yes=0.55, ask_no=0.40, seconds_to_expiry=1800)
    assert engine.score_market(packet).suggested_side == SuggestedSide.NO


def test_distressed_market_blocks_counter_trend_low_ask(tmp_path: Path) -> None:
    """Counter-trend trade blocked when ask on our side is below the distress floor."""
    engine = QuantScoringEngine(_regime_settings(
        tmp_path,
        quant_trend_opposed_strong_min_edge=0.25,
        quant_trend_distressed_max_ask=0.30,
    ))
    # 4h DOWN, scorer picks YES. edge_yes = 0.5 - 0.17 = 0.33 > 0.25 (clears edge bar)
    # but ask_yes = 0.17 < 0.30 floor → distressed block fires.
    packet = _packet(btc_log_return_4h=-0.008, btc_log_return_1h=-0.001,
                     ask_yes=0.17, ask_no=0.88, seconds_to_expiry=1800)
    a = engine.score_market(packet)
    assert a.suggested_side == SuggestedSide.ABSTAIN
    assert any("Distressed" in r for r in a.reasons_to_abstain)


def test_distressed_market_allows_ask_above_floor(tmp_path: Path) -> None:
    """Counter-trend trade with ask above the floor is not distressed-blocked."""
    engine = QuantScoringEngine(_regime_settings(
        tmp_path,
        quant_trend_opposed_strong_min_edge=0.25,
        quant_trend_distressed_max_ask=0.30,
    ))
    # ask_yes = 0.35 > 0.30 floor, edge_yes = 0.5 - 0.35 = 0.15 < 0.25 → edge bar blocks.
    # Use a higher edge setup: ask_yes=0.22 would give 0.28 edge... but > 0.30 floor.
    # ask_yes=0.35, fair≈0.5 → edge=0.15 < 0.25 → edge bar blocks (not distress).
    packet = _packet(btc_log_return_4h=-0.008, btc_log_return_1h=-0.001,
                     ask_yes=0.35, ask_no=0.70, seconds_to_expiry=1800)
    a = engine.score_market(packet)
    assert a.suggested_side == SuggestedSide.ABSTAIN
    assert not any("Distressed" in r for r in a.reasons_to_abstain)  # edge bar, not distress


def test_ofi_gate_blocks_trade_against_flow(tmp_path: Path) -> None:
    """OFI gate vetoes a YES trade when informed flow is strongly bearish."""
    settings = _settings(
        tmp_path,
        quant_slippage_baseline_bps=0.0, quant_slippage_spread_coef=0.0, fee_bps=0.0,
        quant_ofi_gate_enabled=True, quant_ofi_gate_min_abs_flow=30.0,
    )
    engine = QuantScoringEngine(settings)
    # Scorer picks YES but signed_flow is -50 (strong selling) → blocked.
    packet = _packet(ask_yes=0.40, ask_no=0.55, signed_flow_5s=-50.0)
    a = engine.score_market(packet)
    assert a.suggested_side == SuggestedSide.ABSTAIN
    assert any("OFI gate" in r for r in a.reasons_to_abstain)


def test_ofi_gate_allows_trade_with_flow(tmp_path: Path) -> None:
    """OFI gate is a no-op when flow confirms direction."""
    settings = _settings(
        tmp_path,
        quant_slippage_baseline_bps=0.0, quant_slippage_spread_coef=0.0, fee_bps=0.0,
        quant_ofi_gate_enabled=True, quant_ofi_gate_min_abs_flow=30.0,
    )
    engine = QuantScoringEngine(settings)
    # YES trade with bullish flow — passes.
    packet = _packet(ask_yes=0.40, ask_no=0.55, signed_flow_5s=60.0)
    assert engine.score_market(packet).suggested_side == SuggestedSide.YES


def test_vol_regime_gate_extreme_abstains(tmp_path: Path) -> None:
    """Extreme realized vol forces ABSTAIN regardless of edge."""
    settings = _settings(
        tmp_path,
        quant_slippage_baseline_bps=0.0, quant_slippage_spread_coef=0.0, fee_bps=0.0,
        quant_vol_regime_enabled=True,
        quant_vol_regime_high_threshold=0.005,
        quant_vol_regime_extreme_threshold=0.010,
        quant_vol_regime_high_min_edge=0.08,
    )
    engine = QuantScoringEngine(settings)
    packet = _packet(ask_yes=0.40, ask_no=0.55, realized_vol_30m=0.015)
    a = engine.score_market(packet)
    assert a.suggested_side == SuggestedSide.ABSTAIN
    assert any("Vol regime" in r for r in a.reasons_to_abstain)


def test_vol_regime_gate_high_raises_edge_bar(tmp_path: Path) -> None:
    """High vol raises required edge; trade with insufficient edge is blocked."""
    settings = _settings(
        tmp_path,
        quant_slippage_baseline_bps=0.0, quant_slippage_spread_coef=0.0, fee_bps=0.0,
        quant_vol_regime_enabled=True,
        quant_vol_regime_high_threshold=0.005,
        quant_vol_regime_extreme_threshold=0.010,
        quant_vol_regime_high_min_edge=0.08,
    )
    engine = QuantScoringEngine(settings)
    # edge_yes = 0.5 - 0.45 = 0.05 < 0.08 in high vol → blocked.
    packet = _packet(ask_yes=0.45, ask_no=0.55, realized_vol_30m=0.007)
    assert engine.score_market(packet).suggested_side == SuggestedSide.ABSTAIN
    # But a wider edge (0.5 - 0.35 = 0.15 > 0.08) clears the bar.
    packet2 = _packet(ask_yes=0.35, ask_no=0.65, realized_vol_30m=0.007)
    assert engine.score_market(packet2).suggested_side == SuggestedSide.YES


def test_research_from_features_populates_btc_fields(tmp_path: Path, market_candidate) -> None:
    from polymarket_trading_engine.engine.btc_state import BtcSnapshot
    from polymarket_trading_engine.engine.market_state import MarketFeatures

    features = MarketFeatures(
        market_id="m1",
        yes_token_id="yes",
        no_token_id="no",
        bid_yes=0.49,
        ask_yes=0.51,
        bid_no=0.49,
        ask_no=0.51,
        mid_yes=0.5,
        mid_no=0.5,
        microprice_yes=0.505,
        spread_yes=0.02,
        depth_usd_yes=600.0,
        imbalance_top5_yes=0.1,
        last_trade_price_yes=0.50,
        signed_flow_5s=3.0,
        trade_count_5s=4,
        last_update_age_seconds=0.5,
        two_sided=True,
    )
    btc_snapshot = BtcSnapshot(
        price=70000.0,
        observed_at=market_candidate.end_date_iso and __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        log_return_10s=0.0,
        log_return_1m=0.001,
        log_return_5m=0.003,
        log_return_15m=0.005,
        realized_vol_30m=0.02,
        sample_count=120,
    )
    packet = ResearchEngine().build_from_features(
        candidate=market_candidate,
        features=features,
        btc_snapshot=btc_snapshot,
        seconds_to_expiry=1800,
    )
    assert packet.ask_yes == 0.51
    assert packet.imbalance_top5_yes == 0.1
    assert packet.btc_log_return_15m == 0.005
    assert packet.realized_vol_30m == 0.02
    engine = QuantScoringEngine(_settings(tmp_path))
    assessment = engine.score_market(packet)
    assert assessment.fair_probability > 0.5  # positive drift + positive imbalance → YES bias
