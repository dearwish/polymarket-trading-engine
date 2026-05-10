"""Overreaction-fade scorer (adaptive_v2).

Thesis: the fade (GBM) scorer captures *static* disagreement between
Polymarket's mid and the model's fair value. A separate edge exists in
*dynamic* disagreement — when Polymarket's mid moves faster than BTC's
spot move justifies, makers are chasing noise. The mid mean-reverts
toward the BTC-implied level on the next few ticks.

Signal construction:
  - ``pm_move = recent_price_change_bps / 10_000``       (30s Polymarket delta)
  - ``btc_move = btc_log_return_30s``                    (matched 30s BTC spot delta)
  - ``expected_pm_move = btc_move × sensitivity``        (how much PM *should* move)
  - ``overreaction = pm_move − expected_pm_move``        (excess move)

The 30s BTC horizon matches the Polymarket mid-change horizon. The
earlier 5m BTC horizon was disastrous on free-falling markets: BTC's
5m return is heavily smoothed and reads ~0% even while spot is
crashing right now, which made every panic-dump on Polymarket look like
a "pure overreaction" — see the post-mortem on market 2068470 where
adaptive_v2 caught a falling knife three times in 8 minutes.

``sensitivity`` is the conversion from BTC log-return to Polymarket mid
change. At the money (fair=0.5) a 1 % BTC move commonly shifts mid by
5–15 probability points; at the tails the mid barely moves. We take a
fixed calibration constant for simplicity — the alpha is the *sign and
magnitude of the excess*, not the exact coefficient.

Decision:
  - ``|overreaction| < overreaction_threshold``  →  ABSTAIN
  - ``overreaction > 0``  (mid jumped too far up)  →  bet NO  (expect pullback)
  - ``overreaction < 0``  (mid dropped too far)    →  bet YES (expect bounce)
  - ``edge = |overreaction| − cost_floor``         (transacted profit estimate)
  - ``edge > max_abs_edge``                        →  ABSTAIN (suspiciously big)

The scorer is stateless; every call is a pure function of the packet.
All gates (min_edge, min_depth, max_spread, stale_data, TTE buffer,
correlation caps) downstream in the risk engine still apply — this just
replaces the scorer's directional pick.
"""
from __future__ import annotations

from polymarket_trading_engine.types import EvidencePacket, MarketAssessment, SuggestedSide


OVERREACTION_TAG = "overreaction-fade"
# Hard-coded tag consumed by the daemon's maker-routing branch when
# adaptive_v2 is configured to use the paper-maker lifecycle. Kept distinct
# from OVERREACTION_TAG (and from FADE_POST_ONLY_TAG) so analyze_soak can
# attribute maker placements to the correct strategy.
OVERREACTION_POST_ONLY_TAG = "overreaction-post-only-maker"


class OverreactionScorer:
    """Dynamic-disagreement fade. Orthogonal to the GBM fade scorer — it
    triggers on *recent* Polymarket mid moves that outpace their BTC
    justification, where fade triggers on static fair-value gaps.

    Default ``sensitivity=10.0`` means a 1% BTC move is "expected" to
    drive a 10% mid move (i.e. 10 probability-points). Calibrated roughly
    from observation: at a 50¢ market with 5 min TTE and ~0.5% 30m vol,
    dP/d(ln S) ≈ 8–12 per the GBM derivative; 10 is a reasonable center.
    Tune via settings after we see how it performs.
    """

    def __init__(
        self,
        overreaction_threshold: float = 0.02,
        sensitivity: float = 10.0,
        cost_floor: float = 0.005,
        min_seconds_to_expiry: int = 60,
        max_abs_edge: float = 0.30,
        post_only: bool = False,
        ofi_gate_enabled: bool = False,
        ofi_gate_min_abs_flow: float = 0.0,
        invert: bool = False,
        imbalance_gate_enabled: bool = False,
        imbalance_gate_min_abs: float = 0.10,
        min_candle_elapsed_seconds: int = 0,
    ):
        self.overreaction_threshold = overreaction_threshold
        self.sensitivity = sensitivity
        self.cost_floor = cost_floor
        self.min_seconds_to_expiry = min_seconds_to_expiry
        # Hard ceiling on |edge|; suspiciously-large overreactions are
        # historically mis-priced as "noise" when BTC is in fact moving
        # too fast for the 30s window to keep up. Set to 0.0 to disable.
        self.max_abs_edge = max_abs_edge
        # When True, APPROVED assessments stamp ``OVERREACTION_POST_ONLY_TAG``
        # so the daemon routes them through the paper-maker lifecycle
        # (resting limit + TTL) instead of an immediate taker fill. ABSTAIN
        # assessments are unaffected.
        self.post_only = post_only
        # OFI gate: abstain when ``signed_flow_5s`` opposes our chosen side
        # with magnitude ≥ ``ofi_gate_min_abs_flow``. Mirrors the gate in
        # QuantScoringEngine — soak data showed that adverse-flow trades
        # accounted for the bulk of losses on BOTH strategies (winners avg
        # +0.3 flow, losers avg −50.5).
        self.ofi_gate_enabled = ofi_gate_enabled
        self.ofi_gate_min_abs_flow = ofi_gate_min_abs_flow
        # When True, swap the side direction: mid overshot UP → bet YES
        # (continuation) instead of NO (reversion). Mirrors the
        # ``quant_invert_drift`` flip on QuantScoringEngine — same
        # rationale: short-horizon BTC binary moves continue more often
        # than they revert, so the original mean-reversion thesis was the
        # wrong sign of the signal.
        self.invert = invert
        # Top-5 book-imbalance gate. Abstains when the chosen side opposes
        # book pressure with magnitude ≥ ``imbalance_gate_min_abs``. Soak
        # attribution: against-pressure trades bled -$0.17/trade vs +$0.66
        # with-pressure. Asymmetric — never blocks with-pressure entries.
        self.imbalance_gate_enabled = imbalance_gate_enabled
        self.imbalance_gate_min_abs = imbalance_gate_min_abs
        # Candle-phase floor: skip the first N seconds of the candle.
        # Drift-since-open and pm_move signals are unstable at cold start.
        self.min_candle_elapsed_seconds = min_candle_elapsed_seconds

    def score_market(self, packet: EvidencePacket) -> MarketAssessment:
        """Return an APPROVED assessment when the packet shows a
        measurable overreaction, else ABSTAIN with an explanatory reason.
        """
        base = _abstain_template(packet)

        # Pre-market candles have stale books and no BTC-vs-PM
        # relationship yet — the thesis doesn't apply.
        if packet.is_pre_market:
            return _with_reason(base, "Overreaction: pre-market — thesis requires a live candle.")

        # We need the mid-history populated (at least one earlier sample)
        # and a matched-horizon BTC return. Both read zero at cold start;
        # without them we'd manufacture a bogus overreaction signal from
        # nothing. Falls back to btc_log_return_5m only when the 30s
        # field is structurally absent (legacy non-stream packets) — but
        # the daemon's research builder always supplies the 30s value.
        pm_move = float(packet.recent_price_change_bps) / 10_000.0
        btc_move = float(packet.btc_log_return_30s or packet.btc_log_return_5m or 0.0)
        if pm_move == 0.0 and btc_move == 0.0:
            return _with_reason(base, "Overreaction: no mid / BTC delta yet.")

        # Keep away from the last minute — spread blows out, mid is
        # unreliable, and "reversion" has no time to materialise.
        if packet.seconds_to_expiry < self.min_seconds_to_expiry:
            return _with_reason(
                base,
                (
                    f"Overreaction: TTE {packet.seconds_to_expiry}s < min "
                    f"{self.min_seconds_to_expiry}s — reversion can't fire in time."
                ),
            )

        # Candle-phase floor: drift-since-open and pm_move are noisy when
        # the candle has barely opened. Only fires when the packet carries
        # a non-zero ``time_elapsed_in_candle_s`` (threshold markets pass
        # zero and skip this check).
        if (
            self.min_candle_elapsed_seconds > 0
            and packet.time_elapsed_in_candle_s > 0
            and packet.time_elapsed_in_candle_s < self.min_candle_elapsed_seconds
        ):
            return _with_reason(
                base,
                (
                    f"Overreaction: candle elapsed {packet.time_elapsed_in_candle_s}s "
                    f"< min {self.min_candle_elapsed_seconds}s."
                ),
            )

        expected_pm_move = btc_move * self.sensitivity
        overreaction = pm_move - expected_pm_move
        if abs(overreaction) < self.overreaction_threshold:
            return _with_reason(
                base,
                (
                    f"Overreaction: |excess|={abs(overreaction):.4f} < threshold "
                    f"{self.overreaction_threshold:.4f}."
                ),
            )

        # Direction selection. When inverted (continuation thesis): mid
        # overshot upward → bet YES (continues up). Default (reversion):
        # mid overshot upward → bet NO (pulls back).
        overshot_up = overreaction > 0
        if self.invert:
            side = SuggestedSide.YES if overshot_up else SuggestedSide.NO
        else:
            side = SuggestedSide.NO if overshot_up else SuggestedSide.YES

        edge = abs(overreaction) - self.cost_floor
        if edge <= 0.0:
            return _with_reason(
                base,
                (
                    f"Overreaction: |excess|={abs(overreaction):.4f} ≤ cost_floor "
                    f"{self.cost_floor:.4f}; would not recover fees."
                ),
            )
        if self.max_abs_edge > 0.0 and edge > self.max_abs_edge:
            return _with_reason(
                base,
                (
                    f"Overreaction: |edge|={edge:.4f} > ceiling "
                    f"{self.max_abs_edge:.4f} — suspiciously large excess "
                    f"(typically a real BTC move outpacing the 30s window)."
                ),
            )

        # OFI gate: abstain when informed flow opposes our side with
        # significant magnitude. Mirrors QuantScoringEngine's check so
        # both scorers use the same adverse-selection floor. ``flow > 0``
        # means net buying pressure on YES; opposes a NO bet.
        if self.ofi_gate_enabled and self.ofi_gate_min_abs_flow > 0.0:
            flow = float(packet.signed_flow_5s)
            if abs(flow) >= self.ofi_gate_min_abs_flow:
                flow_bullish = flow > 0.0
                if (side is SuggestedSide.YES and not flow_bullish) or (
                    side is SuggestedSide.NO and flow_bullish
                ):
                    return _with_reason(
                        base,
                        f"OFI gate: flow {flow:+.1f} opposes {side.value}.",
                    )

        # Imbalance gate: abstain when top-5 book pressure opposes the
        # chosen side with magnitude ≥ ``imbalance_gate_min_abs``.
        # ``imbalance_top5_yes > 0`` means YES side has more depth (bullish
        # pressure); opposes a NO bet.
        if self.imbalance_gate_enabled and self.imbalance_gate_min_abs > 0.0:
            imb = float(packet.imbalance_top5_yes)
            if abs(imb) >= self.imbalance_gate_min_abs:
                yes_pressure = imb > 0.0
                if (side is SuggestedSide.YES and not yes_pressure) or (
                    side is SuggestedSide.NO and yes_pressure
                ):
                    return _with_reason(
                        base,
                        f"Imbalance gate: top5 {imb:+.3f} opposes {side.value}.",
                    )

        # Fair-probability for the SCORER's frame — the direction we're
        # fading toward. If we're buying YES because mid overshot down,
        # the "fair yes" is higher than the current mid; we express that
        # by shifting current mid by the edge magnitude, capped in (0,1).
        current_mid = float(packet.orderbook_midpoint or 0.5)
        if side is SuggestedSide.YES:
            fair_yes = max(0.01, min(0.99, current_mid + abs(overreaction)))
        else:
            fair_yes = max(0.01, min(0.99, current_mid - abs(overreaction)))

        approved_tag = OVERREACTION_POST_ONLY_TAG if self.post_only else OVERREACTION_TAG
        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=fair_yes,
            fair_probability_no=round(1.0 - fair_yes, 6),
            confidence=0.60,
            suggested_side=side,
            expiry_risk="LOW",
            reasons_for_trade=[
                (
                    f"Overreaction fade: pm_move={pm_move:+.4f} vs "
                    f"btc-implied {expected_pm_move:+.4f}, "
                    f"excess={overreaction:+.4f}; bet {side.value}."
                )
            ],
            reasons_to_abstain=[],
            edge=edge,
            edge_yes=edge if side is SuggestedSide.YES else 0.0,
            edge_no=edge if side is SuggestedSide.NO else 0.0,
            raw_model_output=approved_tag,
            slippage_bps=10.0,
        )


def _abstain_template(packet: EvidencePacket) -> MarketAssessment:
    """Shared ABSTAIN skeleton — every branch fills only reasons_to_abstain."""
    return MarketAssessment(
        market_id=packet.market_id,
        fair_probability=float(packet.orderbook_midpoint or 0.5),
        fair_probability_no=round(1.0 - float(packet.orderbook_midpoint or 0.5), 6),
        confidence=0.0,
        suggested_side=SuggestedSide.ABSTAIN,
        expiry_risk="UNKNOWN",
        reasons_for_trade=[],
        reasons_to_abstain=[],
        edge=0.0,
        edge_yes=0.0,
        edge_no=0.0,
        raw_model_output=OVERREACTION_TAG,
        slippage_bps=0.0,
    )


def _with_reason(base: MarketAssessment, reason: str) -> MarketAssessment:
    """Frozen-dataclass-friendly helper to stamp a single abstain reason
    onto the shared template without mutating shared state."""
    from dataclasses import replace

    return replace(base, reasons_to_abstain=[reason, *base.reasons_to_abstain])
