from __future__ import annotations

import json
import re
from dataclasses import asdict
from textwrap import dedent

import httpx
from pydantic import BaseModel, Field, ValidationError

from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.engine.quant_scoring import QuantScoringEngine
from polymarket_trading_engine.types import EvidencePacket, MarketAssessment, SuggestedSide


class OpenRouterAssessment(BaseModel):
    fair_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons_for_trade: list[str]
    reasons_to_abstain: list[str]
    expiry_risk: str
    suggested_side: SuggestedSide


class ScoringEngine:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)
        self.quant = QuantScoringEngine(settings)

    def score_market(self, packet: EvidencePacket) -> MarketAssessment:
        if self.settings.openrouter_api_key:
            return self._score_via_openrouter(packet)
        return self.quant.score_market(packet)

    def _score_via_openrouter(self, packet: EvidencePacket) -> MarketAssessment:
        prompt = dedent(
            f"""
            You are scoring a Polymarket binary market.
            Return JSON only with keys:
            fair_probability, confidence, reasons_for_trade, reasons_to_abstain, expiry_risk, suggested_side.

            Evidence:
            {json.dumps(asdict(packet), default=str)}
            """
        ).strip()
        response = self.client.post(
            f"{self.settings.openrouter_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.openrouter_model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        body = response.json()
        raw = body["choices"][0]["message"]["content"]
        try:
            parsed = OpenRouterAssessment.model_validate(
                self._normalize_openrouter_payload(json.loads(raw), packet)
            )
        except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as exc:
            return self._score_as_invalid_model_response(packet, raw, str(exc))
        fair = float(parsed.fair_probability)
        edge = fair - packet.market_probability
        suggested_side = self._align_suggested_side(parsed.suggested_side, edge)
        breakdown = self.quant._edge_breakdown(packet, fair)
        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=fair,
            confidence=float(parsed.confidence),
            suggested_side=suggested_side,
            expiry_risk=str(parsed.expiry_risk),
            reasons_for_trade=list(parsed.reasons_for_trade),
            reasons_to_abstain=list(parsed.reasons_to_abstain),
            edge=edge,
            raw_model_output=raw,
            edge_yes=round(breakdown.edge_yes, 6),
            edge_no=round(breakdown.edge_no, 6),
            fair_probability_no=round(1.0 - fair, 6),
            slippage_bps=round(breakdown.slippage_bps, 4),
        )

    def _normalize_openrouter_payload(self, payload: dict, packet: EvidencePacket) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("Model payload must be a JSON object.")
        normalized = dict(payload)
        normalized["fair_probability"] = float(payload["fair_probability"])
        normalized["confidence"] = self._normalize_confidence(payload["confidence"])
        normalized["reasons_for_trade"] = self._normalize_reason_list(payload.get("reasons_for_trade", []))
        normalized["reasons_to_abstain"] = self._normalize_reason_list(payload.get("reasons_to_abstain", []))
        normalized["expiry_risk"] = str(payload.get("expiry_risk", "UNKNOWN"))
        normalized["suggested_side"] = self._normalize_suggested_side(
            payload["suggested_side"],
            fair_probability=normalized["fair_probability"],
            market_probability=packet.market_probability,
        )
        return normalized

    @staticmethod
    def _normalize_reason_list(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if value in (None, ""):
            return []
        return [str(value)]

    @staticmethod
    def _normalize_confidence(value: object) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().lower()
        if not text:
            raise ValueError("confidence is empty")
        percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if percent_match:
            return float(percent_match.group(1)) / 100.0
        number_match = re.search(r"\b(0(?:\.\d+)?|1(?:\.0+)?)\b", text)
        if number_match:
            return float(number_match.group(1))
        if "low to moderate" in text:
            return 0.45
        if "moderate to high" in text:
            return 0.7
        if "high" in text:
            return 0.85
        if "moderate" in text or "medium" in text:
            return 0.6
        if "low" in text:
            return 0.3
        raise ValueError(f"Unrecognized confidence value: {value}")

    @staticmethod
    def _normalize_suggested_side(value: object, fair_probability: float, market_probability: float) -> SuggestedSide:
        if isinstance(value, SuggestedSide):
            return value
        text = str(value).strip().lower()
        if not text:
            raise ValueError("suggested_side is empty")
        compact = re.sub(r"[^a-z]+", " ", text).strip()
        yes_tokens = {"yes", "buy", "buy yes", "long", "long yes"}
        no_tokens = {"no", "sell", "buy no", "sell yes", "sell no", "short", "short yes", "long no"}
        abstain_tokens = {
            "abstain",
            "avoid",
            "hold",
            "skip",
            "no trade",
            "do not trade",
            "pass",
        }
        if compact in abstain_tokens:
            return SuggestedSide.ABSTAIN
        if compact in yes_tokens or re.search(r"\byes\b", compact):
            return SuggestedSide.YES
        if compact in no_tokens or re.search(r"\bno\b", compact):
            return SuggestedSide.NO
        raise ValueError(f"Unrecognized suggested_side value: {value}")

    @staticmethod
    def _align_suggested_side(suggested_side: SuggestedSide, edge: float) -> SuggestedSide:
        if abs(edge) < 1e-9:
            return SuggestedSide.ABSTAIN
        edge_side = SuggestedSide.YES if edge > 0 else SuggestedSide.NO
        if suggested_side == SuggestedSide.ABSTAIN:
            return edge_side
        if suggested_side != edge_side:
            return edge_side
        return suggested_side

    def _score_as_invalid_model_response(
        self,
        packet: EvidencePacket,
        raw_model_output: str,
        error_detail: str,
    ) -> MarketAssessment:
        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=packet.market_probability,
            confidence=0.0,
            suggested_side=SuggestedSide.ABSTAIN,
            expiry_risk="UNKNOWN",
            reasons_for_trade=[],
            reasons_to_abstain=[
                "Model response failed schema validation.",
                f"Validation detail: {error_detail}",
            ],
            edge=0.0,
            raw_model_output=raw_model_output,
            edge_yes=0.0,
            edge_no=0.0,
            fair_probability_no=round(1.0 - packet.market_probability, 6),
            slippage_bps=0.0,
        )
