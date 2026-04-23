"""Regime-gated scorer.

Phase 2+3 of the adaptive-regime branch. The legacy GBM fade scorer wins
in choppy, mean-reverting regimes (Asia overnight) and loses in trending
regimes (EU/US during news flow). This wrapper classifies the tick's
regime from existing HTF features and routes:

- RANGING         → delegate to the underlying fade scorer unchanged
                    (this is the regime where buying the cheap side pays off)
- TRENDING_UP     → follow with maker: side=YES, raw_model_output flagged
                    so the daemon routes this to the paper-maker lifecycle
                    instead of the normal taker execute path
- TRENDING_DOWN   → follow with maker: side=NO (mirror)
- HIGH_VOL        → ABSTAIN (even a real trend gets chopped up here)
- UNKNOWN         → ABSTAIN (HTF buffer hasn't warmed up; no honest signal yet)

The wrapper is deliberately thin — it does not re-implement edge math or
confidence, just short-circuits the side decision and tags the
``raw_model_output`` so downstream execution can distinguish fade
(normal taker entry) from adaptive-follow (paper-maker rest). Because
``edge`` is set to 0 on the follow branch, the risk engine's min_edge
gate naturally blocks any accidental taker routing — the follow path
is only honored by the daemon's maker-aware code path.
"""
from __future__ import annotations

from dataclasses import replace

from polymarket_ai_agent.engine.quant_scoring import QuantScoringEngine
from polymarket_ai_agent.engine.regime import Regime, RegimeThresholds, classify_regime
from polymarket_ai_agent.types import EvidencePacket, MarketAssessment, SuggestedSide


# Hard-coded tag consumed by the daemon's maker-routing branch. Keep in
# sync with the daemon's constant of the same name.
ADAPTIVE_FOLLOW_MAKER_TAG = "adaptive-follow-maker"

_TRADEABLE_REGIMES: frozenset[Regime] = frozenset({Regime.RANGING})
_FOLLOW_REGIMES: frozenset[Regime] = frozenset(
    {Regime.TRENDING_UP, Regime.TRENDING_DOWN}
)


class AdaptiveScorer:
    """Regime-gated wrapper around :class:`QuantScoringEngine`.

    Holds no state beyond the regime thresholds — every call is derived
    from the ``EvidencePacket`` so the scorer is safe to call on every
    tick and reuses the underlying fade scorer's settings (edge gates,
    slippage model, confidence) verbatim.
    """

    def __init__(
        self,
        fade: QuantScoringEngine,
        thresholds: RegimeThresholds | None = None,
    ):
        self.fade = fade
        self.thresholds = thresholds or RegimeThresholds()

    def score_market(self, packet: EvidencePacket) -> MarketAssessment:
        """Return an assessment for ``packet``, routing by regime.

        - RANGING: delegate to fade unchanged.
        - TRENDING_UP / TRENDING_DOWN: follow-with-maker (side picked to
          match the trend; edge/confidence zeroed so only the daemon's
          maker-routing code path acts on it).
        - HIGH_VOL / UNKNOWN: ABSTAIN.

        Preserves the underlying fair_probability and per-side edges so
        downstream telemetry stays comparable across scorers. Only the
        side decision, edge, confidence, reasons_*, and raw_model_output
        get rewritten.
        """
        base = self.fade.score_market(packet)
        # Pre-market: the candle hasn't opened yet, so the market's ASK
        # hasn't settled into a steady state and our maker TTL could
        # straddle the candle open. The follow-maker thesis relies on
        # catching pullbacks INSIDE a live candle's trend — neither
        # applies here. Abstain regardless of HTF regime.
        if packet.is_pre_market:
            return replace(
                base,
                suggested_side=SuggestedSide.ABSTAIN,
                edge=0.0,
                confidence=0.0,
                reasons_for_trade=[],
                reasons_to_abstain=[
                    "Pre-market: candle hasn't opened; adaptive scorer holds fire.",
                    *base.reasons_to_abstain,
                ],
                raw_model_output="adaptive-regime-gated",
            )
        regime = classify_regime(packet, thresholds=self.thresholds)
        if regime in _TRADEABLE_REGIMES:
            return base

        if regime in _FOLLOW_REGIMES:
            follow_side = (
                SuggestedSide.YES if regime is Regime.TRENDING_UP else SuggestedSide.NO
            )
            reason = (
                f"Regime {regime.value}: follow with maker at mid − configured discount."
            )
            return replace(
                base,
                suggested_side=follow_side,
                edge=0.0,
                confidence=0.0,
                reasons_for_trade=[reason],
                reasons_to_abstain=[],
                raw_model_output=ADAPTIVE_FOLLOW_MAKER_TAG,
            )

        gate_reason = f"Regime {regime.value}: adaptive scorer holds fire outside RANGING."
        return replace(
            base,
            suggested_side=SuggestedSide.ABSTAIN,
            edge=0.0,
            confidence=0.0,
            reasons_for_trade=[],
            reasons_to_abstain=[gate_reason, *base.reasons_to_abstain],
            raw_model_output="adaptive-regime-gated",
        )
