from __future__ import annotations

import httpx


class ExternalFeedConnector:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=10)

    def get_btc_price(self) -> float:
        response = self.client.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
        response.raise_for_status()
        payload = response.json()
        return float(payload["price"])
