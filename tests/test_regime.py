from __future__ import annotations

from polymarket_ai_agent.engine.regime import Regime, RegimeThresholds, classify_regime
from polymarket_ai_agent.types import EvidencePacket


def _packet(**overrides) -> EvidencePacket:
    defaults = dict(
        market_id="m",
        question="",
        resolution_criteria="",
        market_probability=0.5,
        orderbook_midpoint=0.5,
        spread=0.01,
        depth_usd=200.0,
        seconds_to_expiry=600,
        external_price=0.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
    )
    defaults.update(overrides)
    return EvidencePacket(**defaults)


def test_unknown_when_htf_buffer_empty() -> None:
    """Cold-start: no HTF bars yet → zero 1h and 4h returns. Must return
    UNKNOWN so the strategy layer can gate on it rather than treating
    (0, 0) as ranging and trading blindly.
    """
    assert classify_regime(_packet()) == Regime.UNKNOWN


def test_high_vol_dominates_trend() -> None:
    """Strong 4h uptrend AND high vol → HIGH_VOL wins.

    This is the load-bearing invariant: chop inside a rally is the worst
    case for a momentum bot, and a volatile 1h reversal can fire even when
    the 4h still shows the old direction.
    """
    packet = _packet(
        btc_log_return_1h=0.005,
        btc_log_return_4h=0.010,
        realized_vol_30m=0.006,  # above default vol_high=0.005
    )
    assert classify_regime(packet) == Regime.HIGH_VOL


def test_trending_up_requires_agreement() -> None:
    packet = _packet(
        btc_log_return_1h=0.002,
        btc_log_return_4h=0.004,
        realized_vol_30m=0.002,
    )
    assert classify_regime(packet) == Regime.TRENDING_UP


def test_trending_down_mirror() -> None:
    packet = _packet(
        btc_log_return_1h=-0.002,
        btc_log_return_4h=-0.004,
        realized_vol_30m=0.002,
    )
    assert classify_regime(packet) == Regime.TRENDING_DOWN


def test_disagreement_is_ranging_not_trending() -> None:
    """1h and 4h with opposite signs → 4h trend is reverting, treat as
    ranging. Labelling this as TRENDING_{UP,DOWN} would front-run a
    reversal the HTF window hasn't yet absorbed.
    """
    packet = _packet(
        btc_log_return_1h=-0.002,
        btc_log_return_4h=0.004,
        realized_vol_30m=0.002,
    )
    assert classify_regime(packet) == Regime.RANGING


def test_weak_trend_is_ranging() -> None:
    """|4h| below trend_min → even with agreement, regime is ranging."""
    packet = _packet(
        btc_log_return_1h=0.001,
        btc_log_return_4h=0.001,
        realized_vol_30m=0.002,
    )
    assert classify_regime(packet) == Regime.RANGING


def test_thresholds_are_overridable() -> None:
    """A caller tuning for a tighter vol definition should be able to
    pass its own thresholds without forking the classifier.
    """
    packet = _packet(
        btc_log_return_1h=0.002,
        btc_log_return_4h=0.004,
        realized_vol_30m=0.002,
    )
    tight = RegimeThresholds(trend_min_abs_4h=0.003, vol_high=0.001)
    assert classify_regime(packet, thresholds=tight) == Regime.HIGH_VOL
