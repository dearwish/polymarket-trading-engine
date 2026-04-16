from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_ai_agent.connectors.external_feeds import ExternalFeedConnector
from polymarket_ai_agent.connectors.polymarket import PolymarketConnector


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class DummyClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return DummyResponse(self.payloads.pop(0))


def test_external_feed_connector_returns_btc_price() -> None:
    client = DummyClient([{"price": "123456.78"}])
    connector = ExternalFeedConnector(client=client)
    assert connector.get_btc_price() == 123456.78


def test_polymarket_connector_discovers_btc_market(settings) -> None:
    payload = [
        {
            "id": "123",
            "question": "Will Bitcoin be up or down in 5 minutes?",
            "conditionId": "cond-123",
            "slug": "btc-5m",
            "endDate": "2099-01-01T00:00:00Z",
            "clobTokenIds": '["yes-token","no-token"]',
            "outcomePrices": "[0.55,0.45]",
            "liquidityNum": 1000,
            "volume24hr": 5000,
            "description": "Resolution text",
        },
        {
            "id": "999",
            "question": "Will BTC be above 120,000 by the end of the month?",
            "conditionId": "cond-999",
            "slug": "btc-monthly",
            "endDate": "2099-01-01T00:00:00Z",
            "clobTokenIds": '["a","b"]',
            "outcomePrices": "[0.5,0.5]",
        },
    ]
    connector = PolymarketConnector(settings, client=DummyClient([payload]))
    markets = connector.discover_markets()
    assert len(markets) == 1
    assert markets[0].market_id == "123"
    assert markets[0].implied_probability == 0.55


def test_polymarket_connector_prefers_nearest_active_btc_5m_market(settings) -> None:
    payload = [
        {
            "id": "nearer",
            "question": "Will Bitcoin be up or down in 5 minutes?",
            "conditionId": "cond-nearer",
            "slug": "btc-nearer",
            "endDate": (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
            "clobTokenIds": '["yes-nearer","no-nearer"]',
            "outcomePrices": "[0.51,0.49]",
            "liquidityNum": 1000,
            "volume24hr": 2000,
            "description": "Resolution text",
        },
        {
            "id": "later",
            "question": "Will BTC be up or down in 5 minutes?",
            "conditionId": "cond-later",
            "slug": "btc-later",
            "endDate": (datetime.now(timezone.utc) + timedelta(minutes=8)).isoformat(),
            "clobTokenIds": '["yes-later","no-later"]',
            "outcomePrices": "[0.52,0.48]",
            "liquidityNum": 5000,
            "volume24hr": 9000,
            "description": "Resolution text",
        },
    ]
    connector = PolymarketConnector(settings, client=DummyClient([payload]))
    market = connector.discover_active_market()
    assert market is not None
    assert market.market_id == "nearer"


def test_polymarket_connector_builds_orderbook_snapshot(settings) -> None:
    client = DummyClient(
        [
            {
                "bids": [{"price": "0.51", "size": "100"}, {"price": "0.50", "size": "50"}],
                "asks": [{"price": "0.53", "size": "100"}, {"price": "0.54", "size": "50"}],
            }
        ]
    )
    connector = PolymarketConnector(settings, client=client)
    snapshot = connector.get_orderbook_snapshot("yes-token")
    assert snapshot.bid == 0.51
    assert snapshot.ask == 0.53
    assert snapshot.midpoint == 0.52
    assert snapshot.spread == 0.02
    assert snapshot.depth_usd > 0


def test_polymarket_connector_estimates_seconds_to_expiry(settings) -> None:
    connector = PolymarketConnector(settings, client=DummyClient([]))
    end_date = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    seconds = connector.estimate_seconds_to_expiry(end_date)
    assert 0 < seconds <= 90


def test_polymarket_connector_reports_missing_live_auth(settings) -> None:
    connector = PolymarketConnector(settings, client=DummyClient([]))
    auth = connector.get_auth_status()
    assert not auth.live_client_constructible
    assert "polymarket_private_key" in auth.missing


def test_polymarket_connector_reports_constructible_live_auth(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
            "polymarket_signature_type": 1,
            "polymarket_funder": "0x" + "2" * 40,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))
    auth = connector.get_auth_status()
    assert auth.live_client_constructible
    client = connector.build_live_client()
    assert client is not None
