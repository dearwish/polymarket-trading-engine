from __future__ import annotations

from polymarket_ai_agent.types import EvidencePacket, MarketSnapshot


class ResearchEngine:
    def build_evidence_packet(self, snapshot: MarketSnapshot) -> EvidencePacket:
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
            orderbook_midpoint=snapshot.orderbook.midpoint,
            spread=snapshot.orderbook.spread,
            depth_usd=snapshot.orderbook.depth_usd,
            seconds_to_expiry=snapshot.seconds_to_expiry,
            external_price=snapshot.external_price,
            recent_price_change_bps=snapshot.recent_price_change_bps,
            recent_trade_count=snapshot.recent_trade_count,
            reasons_context=context,
            citations=citations,
        )
