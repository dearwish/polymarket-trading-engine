from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.types import MarketCandidate, OrderBookSnapshot


class PolymarketConnector:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)

    def discover_markets(self, limit: int = 25) -> list[MarketCandidate]:
        params = {
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }
        response = self.client.get(f"{self.settings.polymarket_gamma_url}/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        markets = [candidate for item in payload if (candidate := self._parse_market(item))]
        return self._sort_market_candidates(markets)

    def discover_active_market(self, limit: int = 50) -> MarketCandidate | None:
        markets = self.discover_markets(limit=limit)
        return markets[0] if markets else None

    def get_market(self, market_id: str) -> MarketCandidate:
        response = self.client.get(f"{self.settings.polymarket_gamma_url}/markets/{market_id}")
        response.raise_for_status()
        candidate = self._parse_market(response.json())
        if not candidate:
            raise ValueError(f"Unable to parse market {market_id}")
        return candidate

    def get_orderbook_snapshot(self, token_id: str) -> OrderBookSnapshot:
        response = self.client.get(f"{self.settings.polymarket_host}/book", params={"token_id": token_id})
        response.raise_for_status()
        data = response.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        midpoint = round((best_bid + best_ask) / 2, 6) if best_bid and best_ask else best_bid or best_ask
        spread = round(max(best_ask - best_bid, 0.0), 6) if best_bid and best_ask else 1.0
        bid_depth = sum(float(level["price"]) * float(level["size"]) for level in bids[:5])
        ask_depth = sum(float(level["price"]) * float(level["size"]) for level in asks[:5])
        return OrderBookSnapshot(
            bid=best_bid,
            ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            depth_usd=bid_depth + ask_depth,
            last_trade_price=midpoint,
        )

    def estimate_seconds_to_expiry(self, end_date_iso: str) -> int:
        try:
            expiry = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        except ValueError:
            return -1
        return int((expiry - datetime.now(timezone.utc)).total_seconds())

    def _parse_market(self, item: dict[str, Any]) -> MarketCandidate | None:
        token_ids = self._parse_token_ids(item.get("clobTokenIds"))
        if len(token_ids) < 2:
            return None
        question = item.get("question") or ""
        if self.settings.market_family == "btc_5m" and not self._matches_btc_5m_market(item):
            return None
        yes_price, no_price = self._parse_outcome_prices(item.get("outcomePrices"))
        implied = yes_price if yes_price else 0.5
        return MarketCandidate(
            market_id=str(item.get("id", "")),
            question=question,
            condition_id=item.get("conditionId", "") or "",
            slug=item.get("slug", "") or "",
            end_date_iso=item.get("endDate", "") or "",
            yes_token_id=token_ids[0],
            no_token_id=token_ids[1],
            implied_probability=implied,
            liquidity_usd=float(item.get("liquidityNum") or item.get("liquidityClob") or 0.0),
            volume_24h_usd=float(item.get("volume24hr") or item.get("volume24hrClob") or 0.0),
            resolution_source=item.get("description") or "",
        )

    @staticmethod
    def _sort_market_candidates(markets: list[MarketCandidate]) -> list[MarketCandidate]:
        def sort_key(candidate: MarketCandidate) -> tuple[int, float, float]:
            seconds_to_expiry = PolymarketConnector._seconds_to_expiry(candidate.end_date_iso)
            effective_expiry = seconds_to_expiry if seconds_to_expiry >= 0 else 10**9
            return (effective_expiry, -candidate.volume_24h_usd, -candidate.liquidity_usd)

        return sorted(markets, key=sort_key)

    @staticmethod
    def _seconds_to_expiry(end_date_iso: str) -> int:
        try:
            expiry = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        except ValueError:
            return -1
        return int((expiry - datetime.now(timezone.utc)).total_seconds())

    @staticmethod
    def _matches_btc_5m_market(item: dict[str, Any]) -> bool:
        haystacks = [
            str(item.get("question") or ""),
            str(item.get("description") or ""),
            str(item.get("slug") or ""),
        ]
        joined = " ".join(haystacks).lower()
        has_btc = "bitcoin" in joined or "btc" in joined
        has_short_window = "5 minutes" in joined or "five minutes" in joined or re.search(r"\b5m\b", joined) is not None
        has_direction = "up or down" in joined or "above or below" in joined or "higher or lower" in joined
        return has_btc and has_short_window and has_direction

    @staticmethod
    def _parse_token_ids(raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            return [str(value) for value in raw_value]
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                stripped = stripped[1:-1]
            return [part.strip().strip('"') for part in stripped.split(",") if part.strip()]
        return []

    @staticmethod
    def _parse_outcome_prices(raw_value: Any) -> tuple[float, float]:
        if isinstance(raw_value, list) and len(raw_value) >= 2:
            return float(raw_value[0]), float(raw_value[1])
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                stripped = stripped[1:-1]
            parts = [part.strip().strip('"') for part in stripped.split(",") if part.strip()]
            if len(parts) >= 2:
                return float(parts[0]), float(parts[1])
        return 0.0, 0.0
