from __future__ import annotations

from polymarket_ai_agent.engine.research import ResearchEngine


def test_research_engine_builds_evidence_packet(market_snapshot) -> None:
    engine = ResearchEngine()
    packet = engine.build_evidence_packet(market_snapshot)
    assert packet.market_id == market_snapshot.candidate.market_id
    assert packet.market_probability == market_snapshot.candidate.implied_probability
    assert len(packet.reasons_context) >= 2
    assert "Binance BTCUSDT ticker" in packet.citations
