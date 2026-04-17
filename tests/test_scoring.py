from __future__ import annotations

import json

from polymarket_ai_agent.engine.research import ResearchEngine
from polymarket_ai_agent.engine.scoring import ScoringEngine
from polymarket_ai_agent.types import SuggestedSide


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class DummyClient:
    def __init__(self, content: str | None = None):
        self.content = content

    def post(self, *args, **kwargs):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": self.content
                        or json.dumps(
                            {
                                "fair_probability": 0.61,
                                "confidence": 0.84,
                                "reasons_for_trade": ["Momentum and external price align."],
                                "reasons_to_abstain": [],
                                "expiry_risk": "LOW",
                                "suggested_side": "YES",
                            }
                        )
                    }
                }
            ]
        }
        return DummyResponse(payload)


def test_scoring_engine_quant_fallback(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    engine = ScoringEngine(settings)
    assessment = engine.score_market(packet)
    assert assessment.market_id == packet.market_id
    assert assessment.raw_model_output == "quant-scoring"
    assert assessment.suggested_side in {SuggestedSide.YES, SuggestedSide.NO, SuggestedSide.ABSTAIN}
    # Per-side edges are always populated by the quant scorer.
    assert abs(assessment.fair_probability + assessment.fair_probability_no - 1.0) < 1e-6


def test_scoring_engine_openrouter_path(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(configured, client=DummyClient())
    assessment = engine.score_market(packet)
    assert assessment.fair_probability == 0.61
    assert assessment.confidence == 0.84
    assert assessment.suggested_side == SuggestedSide.YES


def test_scoring_engine_invalid_json_abstains(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(configured, client=DummyClient(content="{not-json"))
    assessment = engine.score_market(packet)
    assert assessment.suggested_side == SuggestedSide.ABSTAIN
    assert assessment.confidence == 0.0
    assert assessment.edge == 0.0
    assert "schema validation" in assessment.reasons_to_abstain[0].lower()


def test_scoring_engine_missing_fields_abstains(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(
        configured,
        client=DummyClient(content=json.dumps({"fair_probability": 0.6, "confidence": 0.7})),
    )
    assessment = engine.score_market(packet)
    assert assessment.suggested_side == SuggestedSide.ABSTAIN
    assert assessment.fair_probability == packet.market_probability


def test_scoring_engine_bad_enum_abstains(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(
        configured,
        client=DummyClient(
            content=json.dumps(
                {
                    "fair_probability": 0.61,
                    "confidence": 0.84,
                    "reasons_for_trade": ["Momentum and external price align."],
                    "reasons_to_abstain": [],
                    "expiry_risk": "LOW",
                    "suggested_side": "MAYBE",
                }
            )
        ),
    )
    assessment = engine.score_market(packet)
    assert assessment.suggested_side == SuggestedSide.ABSTAIN


def test_scoring_engine_normalizes_string_confidence_and_side(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(
        configured,
        client=DummyClient(
            content=json.dumps(
                {
                    "fair_probability": 0.49,
                    "confidence": "Low to moderate",
                    "reasons_for_trade": ["Potential downside edge."],
                    "reasons_to_abstain": ["Wide spread."],
                    "expiry_risk": "HIGH",
                    "suggested_side": "No - market looks overpriced",
                }
            )
        ),
    )
    assessment = engine.score_market(packet)
    assert assessment.confidence == 0.45
    assert assessment.suggested_side == SuggestedSide.NO
    assert assessment.fair_probability == 0.49


def test_scoring_engine_aligns_side_with_negative_edge(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(
        configured,
        client=DummyClient(
            content=json.dumps(
                {
                    "fair_probability": 0.40,
                    "confidence": "medium",
                    "reasons_for_trade": ["Some downside edge."],
                    "reasons_to_abstain": [],
                    "expiry_risk": "LOW",
                    "suggested_side": "yes",
                }
            )
        ),
    )
    assessment = engine.score_market(packet)
    assert assessment.edge < 0
    assert assessment.suggested_side == SuggestedSide.NO


def test_scoring_engine_normalizes_buy_yes_style_side(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(
        configured,
        client=DummyClient(
            content=json.dumps(
                {
                    "fair_probability": 0.70,
                    "confidence": "high",
                    "reasons_for_trade": ["Positive edge detected."],
                    "reasons_to_abstain": [],
                    "expiry_risk": "LOW",
                    "suggested_side": "buy-yes",
                }
            )
        ),
    )
    assessment = engine.score_market(packet)
    assert assessment.edge > 0
    assert assessment.suggested_side == SuggestedSide.YES


def test_scoring_engine_normalizes_plain_buy_side(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(
        configured,
        client=DummyClient(
            content=json.dumps(
                {
                    "fair_probability": 0.72,
                    "confidence": "high",
                    "reasons_for_trade": ["Positive edge detected."],
                    "reasons_to_abstain": [],
                    "expiry_risk": "LOW",
                    "suggested_side": "buy",
                }
            )
        ),
    )
    assessment = engine.score_market(packet)
    assert assessment.edge > 0
    assert assessment.suggested_side == SuggestedSide.YES


def test_scoring_engine_normalizes_no_trade_to_abstain(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(
        configured,
        client=DummyClient(
            content=json.dumps(
                {
                    "fair_probability": packet.market_probability,
                    "confidence": "medium",
                    "reasons_for_trade": [],
                    "reasons_to_abstain": ["No edge."],
                    "expiry_risk": "LOW",
                    "suggested_side": "no trade",
                }
            )
        ),
    )
    assessment = engine.score_market(packet)
    assert assessment.edge == 0.0
    assert assessment.suggested_side == SuggestedSide.ABSTAIN


def test_scoring_engine_out_of_range_probability_abstains(settings, market_snapshot) -> None:
    packet = ResearchEngine().build_evidence_packet(market_snapshot)
    configured = settings.model_copy(update={"openrouter_api_key": "test-key"})
    engine = ScoringEngine(
        configured,
        client=DummyClient(
            content=json.dumps(
                {
                    "fair_probability": 1.5,
                    "confidence": 0.84,
                    "reasons_for_trade": ["Momentum and external price align."],
                    "reasons_to_abstain": [],
                    "expiry_risk": "LOW",
                    "suggested_side": "YES",
                }
            )
        ),
    )
    assessment = engine.score_market(packet)
    assert assessment.suggested_side == SuggestedSide.ABSTAIN
