from __future__ import annotations

import math
from dataclasses import dataclass

from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.types import EvidencePacket, MarketAssessment, SuggestedSide


_SQRT_1800 = math.sqrt(1800.0)


# Sentinel on ``MarketAssessment.raw_model_output`` that tells the daemon's
# strategy-tick router to push this assessment through the paper-maker
# lifecycle (rest a limit at mid − discount, wait for the book to cross)
# instead of the immediate taker-fill path. Set when ``fade_post_only`` is
# on. Distinct from ``ADAPTIVE_FOLLOW_MAKER_TAG`` so each scorer owns its
# own routing signal; the daemon accepts both.
FADE_POST_ONLY_TAG = "fade-post-only-maker"


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(slots=True)
class EdgeBreakdown:
    fair_yes: float
    ask_yes: float
    ask_no: float
    slippage_bps: float
    fee_bps: float
    edge_yes: float
    edge_no: float


class QuantScoringEngine:
    """Closed-form fair-value scorer for BTC up/down markets.

    Models BTC as a drift-less GBM over the time remaining (τ) and converts the
    normalized log-return since the implicit candle open into an YES-side
    probability. The momentum tilt from the order book top-5 imbalance is added
    as a small linear adjustment. Edges are computed per-side after an explicit
    cost model: baseline taker slippage plus a spread-proportional widening and
    a configurable fee. All outputs are bounded and deterministic, so the
    daemon can call this on every book update without I/O.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def score_market(self, packet: EvidencePacket) -> MarketAssessment:
        fair_yes, fair_reasons = self._fair_value(packet)
        breakdown = self._edge_breakdown(packet, fair_yes)
        side, chosen_edge, side_reasons = self._pick_side(breakdown)

        # Regime gate: replaces the old binary trend veto with a unified check
        # that varies the required minimum edge by regime (trend strength,
        # microstructure flow, and volatility). Counter-trend trades need more
        # edge; with-trend and ranging trades use the normal threshold.
        gate_reason: str | None = None
        if side is not SuggestedSide.ABSTAIN:
            gate_reason = self._regime_gate(packet, side, chosen_edge)
            if gate_reason:
                side = SuggestedSide.ABSTAIN
                chosen_edge = 0.0
                side_reasons = []

        ceiling = float(self.settings.quant_max_abs_edge)
        ceiling_hit = (
            ceiling > 0.0
            and side is not SuggestedSide.ABSTAIN
            and abs(chosen_edge) > ceiling
        )
        if ceiling_hit:
            side = SuggestedSide.ABSTAIN
            side_reasons = []
        gated = gate_reason is not None
        confidence = self._confidence(breakdown, chosen_edge) if not (ceiling_hit or gated) else 0.0
        reasons_for_trade, reasons_to_abstain = self._reasons(
            packet, breakdown, side, chosen_edge, fair_reasons, side_reasons
        )
        # Primary-cause first: when the regime gate or |edge| ceiling forced
        # ABSTAIN, `_reasons` has already prepended the generic "No positive
        # edge after costs …" string (because side was rewritten to ABSTAIN
        # before _reasons ran). Put the real driver at position 0 so
        # downstream consumers — dashboards, analyze_soak — get the binding
        # constraint as the headline reason.
        if gate_reason:
            reasons_to_abstain.insert(0, gate_reason)
        if ceiling_hit:
            reasons_to_abstain.insert(
                0,
                f"Chosen edge {chosen_edge:+.4f} exceeds |edge| ceiling {ceiling:.2f}.",
            )
        expiry_risk = self._expiry_risk(packet)
        raw_model_output = "quant-scoring"
        if (
            bool(self.settings.fade_post_only)
            and side is not SuggestedSide.ABSTAIN
        ):
            raw_model_output = FADE_POST_ONLY_TAG
        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=round(fair_yes, 6),
            confidence=round(confidence, 4),
            suggested_side=side,
            expiry_risk=expiry_risk,
            reasons_for_trade=reasons_for_trade,
            reasons_to_abstain=reasons_to_abstain,
            edge=round(chosen_edge, 6),
            raw_model_output=raw_model_output,
            edge_yes=round(breakdown.edge_yes, 6),
            edge_no=round(breakdown.edge_no, 6),
            fair_probability_no=round(1.0 - fair_yes, 6),
            slippage_bps=round(breakdown.slippage_bps, 4),
        )

    # --- Fair value ----------------------------------------------------

    def _fair_value(self, packet: EvidencePacket) -> tuple[float, list[str]]:
        tte = max(float(packet.seconds_to_expiry), float(self.settings.quant_tte_floor_seconds))
        sigma_per_second = self._sigma_per_second(packet)
        expected_stdev = sigma_per_second * math.sqrt(tte)
        drift = self._drift_log_return(packet)
        z = drift / max(expected_stdev, 1e-9) if expected_stdev > 0 else 0.0
        damping = float(self.settings.quant_drift_damping)
        fair_from_drift = _normal_cdf(z * damping) if drift != 0.0 else 0.5
        imbalance = max(-1.0, min(1.0, float(packet.imbalance_top5_yes)))
        tilt = imbalance * float(self.settings.quant_imbalance_tilt)
        fair_yes = fair_from_drift + tilt
        # Mean-reversion inversion test: flip fair_yes if the flag is set.
        # Applied post-tilt so both drift and imbalance signals reverse sign
        # consistently. Only affects output; all intermediate math stays the same.
        inverted = bool(self.settings.quant_invert_drift)
        if inverted:
            fair_yes = 1.0 - fair_yes
        fair_yes = max(0.01, min(0.99, fair_yes))
        reasons = [
            f"z={z:+.2f} drift={drift:+.5f} σ_per_s={sigma_per_second:.6f} expected_stdev={expected_stdev:.5f}",
            f"imbalance tilt={tilt:+.4f} base_fair={fair_from_drift:.4f} fair_yes={fair_yes:.4f}"
            + (" [inverted]" if inverted else ""),
        ]
        return fair_yes, reasons

    def _sigma_per_second(self, packet: EvidencePacket) -> float:
        if packet.realized_vol_30m > 0:
            return packet.realized_vol_30m / _SQRT_1800
        return float(self.settings.quant_default_vol_per_second)

    def _drift_log_return(self, packet: EvidencePacket) -> float:
        # Pre-market: the market was discovered before its candle opened.
        # Rolling 5m/15m returns measure the wrong thing here (they'd inject
        # a phantom edge from before-window price action that has no bearing
        # on this candle's close-vs-open outcome), so return 0 — scorer
        # falls back to fair = 0.5 plus the imbalance tilt only.
        if packet.is_pre_market:
            return 0.0
        # Directional "Up or Down" candle markets: the right signal is the log
        # return OBSERVED so far since the market's candle opened, not a rolling
        # 5m/15m window. Using rolling windows is structurally wrong here — they
        # measure "BTC now vs BTC N minutes ago" rather than "BTC now vs BTC at
        # candle open", which is what P(close > open) depends on.
        if packet.btc_log_return_since_candle_open != 0.0:
            return float(packet.btc_log_return_since_candle_open)
        # Threshold markets ("above $K"): distance-to-strike ln(S/K).
        if packet.btc_log_return_vs_strike != 0.0:
            return float(packet.btc_log_return_vs_strike)
        # In-candle cold-start: candle has opened but the BTC buffer hasn't
        # accumulated enough samples to reconstruct the candle-open return.
        # The rolling 5m/15m window is a reasonable stand-in here — it at
        # least reflects recent BTC direction.
        horizon = float(self.settings.quant_drift_horizon_seconds)
        if horizon >= 600.0 and packet.btc_log_return_15m != 0.0:
            return float(packet.btc_log_return_15m)
        if packet.btc_log_return_5m != 0.0:
            return float(packet.btc_log_return_5m)
        return float(packet.btc_log_return_15m)

    # --- Edges ---------------------------------------------------------

    def _edge_breakdown(self, packet: EvidencePacket, fair_yes: float) -> EdgeBreakdown:
        ask_yes = self._effective_ask_yes(packet)
        ask_no = self._effective_ask_no(packet)
        slippage_bps = self._slippage_bps(packet)
        fee_bps = float(self.settings.fee_bps)
        cost = (slippage_bps + fee_bps) / 10_000.0
        edge_yes = fair_yes - ask_yes - cost
        edge_no = (1.0 - fair_yes) - ask_no - cost
        return EdgeBreakdown(
            fair_yes=fair_yes,
            ask_yes=ask_yes,
            ask_no=ask_no,
            slippage_bps=slippage_bps,
            fee_bps=fee_bps,
            edge_yes=edge_yes,
            edge_no=edge_no,
        )

    def _effective_ask_yes(self, packet: EvidencePacket) -> float:
        if packet.ask_yes > 0.0:
            return packet.ask_yes
        if packet.orderbook_midpoint > 0.0:
            return min(0.999, packet.orderbook_midpoint + max(packet.spread, 0.0) / 2.0)
        return 1.0

    def _effective_ask_no(self, packet: EvidencePacket) -> float:
        if packet.ask_no > 0.0:
            return packet.ask_no
        # Derive from YES side: ask_no ≈ 1 − bid_yes.
        if packet.bid_yes > 0.0:
            return max(0.001, min(0.999, 1.0 - packet.bid_yes))
        if packet.orderbook_midpoint > 0.0:
            return min(0.999, (1.0 - packet.orderbook_midpoint) + max(packet.spread, 0.0) / 2.0)
        return 1.0

    def _slippage_bps(self, packet: EvidencePacket) -> float:
        baseline = float(self.settings.quant_slippage_baseline_bps)
        spread_bps = max(packet.spread, 0.0) * 10_000.0
        return baseline + spread_bps * float(self.settings.quant_slippage_spread_coef)

    # --- Side + confidence --------------------------------------------

    def _pick_side(self, breakdown: EdgeBreakdown) -> tuple[SuggestedSide, float, list[str]]:
        if breakdown.edge_yes <= 0.0 and breakdown.edge_no <= 0.0:
            return (
                SuggestedSide.ABSTAIN,
                max(breakdown.edge_yes, breakdown.edge_no),
                [],
            )
        if breakdown.edge_yes >= breakdown.edge_no:
            return (
                SuggestedSide.YES,
                breakdown.edge_yes,
                [f"YES edge {breakdown.edge_yes:+.4f} beats NO edge {breakdown.edge_no:+.4f}"],
            )
        return (
            SuggestedSide.NO,
            breakdown.edge_no,
            [f"NO edge {breakdown.edge_no:+.4f} beats YES edge {breakdown.edge_yes:+.4f}"],
        )

    def _regime_gate(self, packet: EvidencePacket, side: SuggestedSide, chosen_edge: float) -> str | None:
        """Return a rejection reason string if the regime blocks this trade, else None.

        Three independent checks run in priority order:
          1. Trend-based minimum edge — counter-trend trades need more edge to pass.
          2. OFI gate — strong informed flow opposing the trade direction is a veto.
          3. Volatility regime — high vol raises the edge bar; extreme vol abstains.

        Note: min/max entry-price gates moved to RiskEngine (2026-04-29) so
        they apply uniformly to fade + adaptive_v2 + any future scorer. The
        scorer no longer ABSTAINs on price — the trade is APPROVED here and
        REJECTED downstream by ``min_entry_price`` / ``max_entry_price`` in
        ``RiskState.rejected_by``.
        """

        # 1. Trend-based minimum edge (replaces the old binary trend veto)
        if bool(self.settings.quant_trend_filter_enabled):
            min_ret = float(self.settings.quant_trend_filter_min_abs_return)
            r4h = float(packet.btc_log_return_4h)
            r1h = float(packet.btc_log_return_1h)
            if abs(r4h) >= min_ret:
                trend_return, label = r4h, "4h"
            elif abs(r1h) >= min_ret:
                trend_return, label = r1h, "1h"
            else:
                trend_return, label = 0.0, ""
            if trend_return != 0.0:
                trend_up = trend_return > 0.0
                opposed = (trend_up and side is SuggestedSide.NO) or (
                    not trend_up and side is SuggestedSide.YES
                )
                if opposed:
                    required = float(
                        self.settings.quant_trend_opposed_strong_min_edge
                        if label == "4h"
                        else self.settings.quant_trend_opposed_weak_min_edge
                    )
                    if chosen_edge < required:
                        trend_dir = "UP" if trend_up else "DOWN"
                        return (
                            f"Regime ({label} {trend_dir} {trend_return:+.4f}): "
                            f"counter-trend edge {chosen_edge:+.4f} < required {required:.4f}."
                        )
                    # Distressed market: market is already heavily priced against our
                    # side (ask is very low). Even with sufficient edge, the GBM model
                    # is structurally late — the market has already priced in the move.
                    max_ask = float(self.settings.quant_trend_distressed_max_ask)
                    if max_ask > 0.0:
                        ask_our_side = float(
                            packet.ask_yes if side is SuggestedSide.YES else packet.ask_no
                        )
                        if ask_our_side < max_ask:
                            trend_dir = "UP" if trend_up else "DOWN"
                            return (
                                f"Distressed ({label} {trend_dir}): "
                                f"{side.value} ask {ask_our_side:.3f} < floor {max_ask:.3f}."
                            )

        # 2. OFI gate: don't trade against strong informed order flow.
        if bool(self.settings.quant_ofi_gate_enabled):
            flow = float(packet.signed_flow_5s)
            ofi_min = float(self.settings.quant_ofi_gate_min_abs_flow)
            if abs(flow) >= ofi_min:
                flow_bullish = flow > 0.0
                if (side is SuggestedSide.YES and not flow_bullish) or (
                    side is SuggestedSide.NO and flow_bullish
                ):
                    return f"OFI gate: flow {flow:+.1f} opposes {side.value}."

        # 3. Volatility regime gate.
        if bool(self.settings.quant_vol_regime_enabled):
            vol = float(packet.realized_vol_30m)
            extreme = float(self.settings.quant_vol_regime_extreme_threshold)
            high = float(self.settings.quant_vol_regime_high_threshold)
            if vol >= extreme:
                return f"Vol regime: realized_vol {vol:.5f} exceeds extreme threshold {extreme:.5f}."
            if vol >= high:
                high_edge = float(self.settings.quant_vol_regime_high_min_edge)
                if chosen_edge < high_edge:
                    return (
                        f"Vol regime: high vol {vol:.5f}, "
                        f"edge {chosen_edge:+.4f} < required {high_edge:.4f}."
                    )

        return None

    def _confidence(self, breakdown: EdgeBreakdown, chosen_edge: float) -> float:
        if chosen_edge <= 0.0:
            return 0.0
        per_edge = float(self.settings.quant_confidence_per_edge)
        conf = 0.5 + per_edge * chosen_edge
        if breakdown.slippage_bps > 100.0:
            conf *= 0.9
        return max(0.0, min(0.99, conf))

    def _expiry_risk(self, packet: EvidencePacket) -> str:
        if packet.seconds_to_expiry <= int(self.settings.quant_high_expiry_risk_seconds):
            return "HIGH"
        if packet.seconds_to_expiry <= int(self.settings.quant_medium_expiry_risk_seconds):
            return "MEDIUM"
        return "LOW"

    def score_shadow(
        self,
        packet: EvidencePacket,
        live: MarketAssessment | None = None,
    ) -> MarketAssessment | None:
        """Compute a shadow assessment for the configured shadow variant.

        Returns None when quant_shadow_variant is empty (disabled). Trades
        continue to use score_market() exclusively; this output is logged
        alongside the base assessment for offline A/B comparison only.
        """
        variant = str(self.settings.quant_shadow_variant)
        if not variant:
            return None
        if variant == "fade_invert_side":
            # Side-flip A/B: mirror live fair_yes (1−p) and flip the chosen
            # side. Inherits the live abstain decision so the chosen-tick
            # Brier delta is computed on the same population — only the
            # side mapping changes.
            if live is None:
                live = self.score_market(packet)
            if live.suggested_side is SuggestedSide.ABSTAIN:
                flipped_side = SuggestedSide.ABSTAIN
            elif live.suggested_side is SuggestedSide.YES:
                flipped_side = SuggestedSide.NO
            else:
                flipped_side = SuggestedSide.YES
            fair_yes = max(0.01, min(0.99, 1.0 - live.fair_probability))
            breakdown = self._edge_breakdown(packet, fair_yes)
            chosen_edge = (
                0.0
                if flipped_side is SuggestedSide.ABSTAIN
                else breakdown.edge_yes
                if flipped_side is SuggestedSide.YES
                else breakdown.edge_no
            )
            return MarketAssessment(
                market_id=packet.market_id,
                fair_probability=round(fair_yes, 6),
                confidence=live.confidence,
                suggested_side=flipped_side,
                expiry_risk=live.expiry_risk,
                reasons_for_trade=[],
                reasons_to_abstain=[],
                edge=round(chosen_edge, 6),
                raw_model_output=f"quant-shadow-{variant}",
                edge_yes=round(breakdown.edge_yes, 6),
                edge_no=round(breakdown.edge_no, 6),
                fair_probability_no=round(1.0 - fair_yes, 6),
                slippage_bps=round(breakdown.slippage_bps, 4),
            )
        base_fair, _ = self._fair_value(packet)
        tilt = 0.0
        if variant == "htf_tilt":
            r1h = float(packet.btc_log_return_1h)
            if r1h != 0.0:
                strength = float(self.settings.quant_shadow_htf_tilt_strength)
                tilt += (1.0 if r1h > 0.0 else -1.0) * strength
            session_biases: dict[str, float] = {
                "eu": float(self.settings.quant_shadow_session_bias_eu),
                "us": float(self.settings.quant_shadow_session_bias_us),
            }
            tilt += session_biases.get(packet.btc_session, 0.0)
        fair_yes = max(0.01, min(0.99, base_fair + tilt))
        breakdown = self._edge_breakdown(packet, fair_yes)
        side, chosen_edge, _ = self._pick_side(breakdown)
        ceiling = float(self.settings.quant_max_abs_edge)
        if ceiling > 0.0 and side is not SuggestedSide.ABSTAIN and abs(chosen_edge) > ceiling:
            side = SuggestedSide.ABSTAIN
            chosen_edge = 0.0
        confidence = self._confidence(breakdown, chosen_edge)
        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=round(fair_yes, 6),
            confidence=round(confidence, 4),
            suggested_side=side,
            expiry_risk=self._expiry_risk(packet),
            reasons_for_trade=[],
            reasons_to_abstain=[],
            edge=round(chosen_edge, 6),
            raw_model_output=f"quant-shadow-{variant}",
            edge_yes=round(breakdown.edge_yes, 6),
            edge_no=round(breakdown.edge_no, 6),
            fair_probability_no=round(1.0 - fair_yes, 6),
            slippage_bps=round(breakdown.slippage_bps, 4),
        )

    def _reasons(
        self,
        packet: EvidencePacket,
        breakdown: EdgeBreakdown,
        side: SuggestedSide,
        chosen_edge: float,
        fair_reasons: list[str],
        side_reasons: list[str],
    ) -> tuple[list[str], list[str]]:
        reasons_for_trade = list(fair_reasons) + list(side_reasons)
        reasons_to_abstain: list[str] = []
        if side == SuggestedSide.ABSTAIN:
            reasons_to_abstain.append(
                f"No positive edge after costs (yes={breakdown.edge_yes:+.4f}, no={breakdown.edge_no:+.4f})."
            )
        if packet.seconds_to_expiry <= int(self.settings.quant_high_expiry_risk_seconds):
            reasons_to_abstain.append("Market is within high-expiry-risk window.")
        if breakdown.slippage_bps > 150.0:
            reasons_to_abstain.append(
                f"Slippage estimate {breakdown.slippage_bps:.0f}bps is high relative to available edge."
            )
        reasons_for_trade.append(
            f"ask_yes={breakdown.ask_yes:.4f} ask_no={breakdown.ask_no:.4f} "
            f"slippage_bps={breakdown.slippage_bps:.1f} fee_bps={breakdown.fee_bps:.1f}"
        )
        return reasons_for_trade, reasons_to_abstain
