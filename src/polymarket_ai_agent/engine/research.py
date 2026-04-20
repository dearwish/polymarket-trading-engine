from __future__ import annotations

import math
import re

from polymarket_ai_agent.engine.btc_state import BtcSnapshot
from polymarket_ai_agent.engine.market_state import MarketFeatures
from polymarket_ai_agent.types import EvidencePacket, MarketCandidate, MarketSnapshot

_STRIKE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


class ResearchEngine:
    """Builds :class:`EvidencePacket` inputs for downstream scoring.

    Two entry points:
      * :py:meth:`build_evidence_packet` consumes the REST-sourced
        :class:`MarketSnapshot` used by the existing synchronous flows.
      * :py:meth:`build_from_features` consumes the websocket-driven
        :class:`MarketFeatures` + :class:`BtcSnapshot` produced by the daemon.

    Both populate the extended packet fields (per-side bids/asks, microprice,
    top-5 imbalance, signed-flow, and BTC log-returns) with safe defaults when
    the upstream data source does not provide them.
    """

    def build_evidence_packet(self, snapshot: MarketSnapshot) -> EvidencePacket:
        orderbook = snapshot.orderbook
        midpoint = orderbook.midpoint
        half_spread = max(orderbook.spread, 0.0) / 2.0
        ask_yes = min(0.999, midpoint + half_spread) if midpoint > 0 else 0.0
        bid_yes = max(0.001, midpoint - half_spread) if midpoint > 0 else 0.0
        ask_no = min(0.999, (1.0 - midpoint) + half_spread) if midpoint > 0 else 0.0
        bid_no = max(0.001, (1.0 - midpoint) - half_spread) if midpoint > 0 else 0.0
        context = [
            f"Market question: {snapshot.candidate.question}",
            f"External BTC price: {snapshot.external_price:.2f}",
            f"Recent price change (bps): {snapshot.recent_price_change_bps:.2f}",
            f"Seconds to expiry: {snapshot.seconds_to_expiry}",
        ]
        citations = [
            snapshot.candidate.slug or snapshot.candidate.market_id,
            "Binance BTCUSDT ticker",
        ]
        return EvidencePacket(
            market_id=snapshot.candidate.market_id,
            question=snapshot.candidate.question,
            resolution_criteria=snapshot.candidate.resolution_source or "No explicit resolution text available.",
            market_probability=snapshot.candidate.implied_probability,
            orderbook_midpoint=midpoint,
            spread=orderbook.spread,
            depth_usd=orderbook.depth_usd,
            seconds_to_expiry=snapshot.seconds_to_expiry,
            external_price=snapshot.external_price,
            recent_price_change_bps=snapshot.recent_price_change_bps,
            recent_trade_count=snapshot.recent_trade_count,
            reasons_context=context,
            citations=citations,
            bid_yes=bid_yes,
            ask_yes=ask_yes,
            bid_no=bid_no,
            ask_no=ask_no,
            microprice_yes=midpoint,
        )

    def build_from_features(
        self,
        candidate: MarketCandidate,
        features: MarketFeatures,
        btc_snapshot: BtcSnapshot | None,
        seconds_to_expiry: int,
        time_elapsed_in_candle_s: int = 0,
        btc_log_return_since_candle_open: float = 0.0,
    ) -> EvidencePacket:
        midpoint = features.mid_yes or candidate.implied_probability
        btc_price = btc_snapshot.price if btc_snapshot else 0.0
        btc_return_5m = btc_snapshot.log_return_5m if btc_snapshot else 0.0
        btc_return_15m = btc_snapshot.log_return_15m if btc_snapshot else 0.0
        realized_vol_30m = btc_snapshot.realized_vol_30m if btc_snapshot else 0.0
        btc_session = btc_snapshot.btc_session if btc_snapshot else "off"
        btc_return_1h = btc_snapshot.btc_log_return_1h if btc_snapshot else 0.0
        btc_return_4h = btc_snapshot.btc_log_return_4h if btc_snapshot else 0.0
        btc_return_24h = btc_snapshot.btc_log_return_24h if btc_snapshot else 0.0
        context = [
            f"Market question: {candidate.question}",
            f"BTC price: {btc_price:.2f}",
            f"BTC log-return (5m): {btc_return_5m:+.5f}",
            f"BTC log-return (15m): {btc_return_15m:+.5f}",
            f"Realized vol (30m): {realized_vol_30m:.5f}",
            f"Imbalance top5 (yes): {features.imbalance_top5_yes:+.3f}",
            f"Signed flow (5s): {features.signed_flow_5s:+.2f} over {features.trade_count_5s} trades",
            f"Seconds to expiry: {seconds_to_expiry}",
            f"WS data age (s): {features.last_update_age_seconds:.2f}",
        ]
        citations = [
            candidate.slug or candidate.market_id,
            "Polymarket CLOB websocket",
            "Binance BTCUSDT websocket",
        ]
        log_return_vs_strike = self._log_return_vs_strike(candidate.question, btc_price)
        return EvidencePacket(
            market_id=candidate.market_id,
            question=candidate.question,
            resolution_criteria=candidate.resolution_source or "No explicit resolution text available.",
            market_probability=candidate.implied_probability,
            orderbook_midpoint=midpoint,
            spread=features.spread_yes,
            depth_usd=features.depth_usd_yes,
            seconds_to_expiry=seconds_to_expiry,
            external_price=btc_price,
            recent_price_change_bps=0.0,
            recent_trade_count=features.trade_count_5s,
            reasons_context=context,
            citations=citations,
            bid_yes=features.bid_yes,
            ask_yes=features.ask_yes,
            bid_no=features.bid_no,
            ask_no=features.ask_no,
            microprice_yes=features.microprice_yes,
            imbalance_top5_yes=features.imbalance_top5_yes,
            signed_flow_5s=features.signed_flow_5s,
            btc_log_return_5m=btc_return_5m,
            btc_log_return_15m=btc_return_15m,
            realized_vol_30m=realized_vol_30m,
            time_elapsed_in_candle_s=time_elapsed_in_candle_s,
            btc_log_return_vs_strike=log_return_vs_strike,
            btc_log_return_since_candle_open=btc_log_return_since_candle_open,
            btc_session=btc_session,
            btc_log_return_1h=btc_return_1h,
            btc_log_return_4h=btc_return_4h,
            btc_log_return_24h=btc_return_24h,
        )

    @staticmethod
    def _log_return_vs_strike(question: str, btc_price: float) -> float:
        """Return ln(btc_price / strike) for threshold questions, 0.0 otherwise."""
        if btc_price <= 0.0:
            return 0.0
        match = _STRIKE_RE.search(question)
        if not match:
            return 0.0
        try:
            strike = float(match.group(1).replace(",", ""))
        except ValueError:
            return 0.0
        if strike <= 0.0:
            return 0.0
        return math.log(btc_price / strike)
