from __future__ import annotations

import json
from dataclasses import asdict
from textwrap import dedent

import httpx

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.types import EvidencePacket, MarketAssessment, SuggestedSide


class ScoringEngine:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)

    def score_market(self, packet: EvidencePacket) -> MarketAssessment:
        if self.settings.openrouter_api_key:
            return self._score_via_openrouter(packet)
        return self._score_heuristically(packet)

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
        parsed = json.loads(raw)
        side = SuggestedSide(parsed["suggested_side"])
        fair = float(parsed["fair_probability"])
        edge = fair - packet.market_probability
        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=fair,
            confidence=float(parsed["confidence"]),
            suggested_side=side,
            expiry_risk=str(parsed["expiry_risk"]),
            reasons_for_trade=list(parsed["reasons_for_trade"]),
            reasons_to_abstain=list(parsed["reasons_to_abstain"]),
            edge=edge,
            raw_model_output=raw,
        )

    def _score_heuristically(self, packet: EvidencePacket) -> MarketAssessment:
        directional_bump = 0.015 if packet.recent_price_change_bps > 0 else -0.015
        external_bias = 0.01 if packet.external_price > 0 else 0.0
        fair_probability = max(0.01, min(0.99, packet.market_probability + directional_bump + external_bias))
        edge = fair_probability - packet.market_probability
        if edge > 0.01:
            side = SuggestedSide.YES
        elif edge < -0.01:
            side = SuggestedSide.NO
        else:
            side = SuggestedSide.ABSTAIN
        reasons_for_trade = [
            "Heuristic fallback scoring is active because no OpenRouter key is configured.",
            f"Recent price change bps = {packet.recent_price_change_bps:.2f}",
        ]
        reasons_to_abstain = []
        if packet.seconds_to_expiry <= 30:
            reasons_to_abstain.append("Market is too close to expiry.")
        if packet.spread > 0.03:
            reasons_to_abstain.append("Spread is wide relative to short-horizon edge.")
        confidence = 0.55 if side == SuggestedSide.ABSTAIN else 0.70
        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=fair_probability,
            confidence=confidence,
            suggested_side=side,
            expiry_risk="HIGH" if packet.seconds_to_expiry <= 30 else "MEDIUM",
            reasons_for_trade=reasons_for_trade,
            reasons_to_abstain=reasons_to_abstain,
            edge=edge,
            raw_model_output="heuristic-fallback",
        )
