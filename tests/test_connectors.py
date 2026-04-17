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


def test_polymarket_connector_ignores_far_expiry_btc_5m_markets(settings) -> None:
    payload = [
        {
            "id": "too-far",
            "question": "Will Bitcoin be up or down in 5 minutes?",
            "conditionId": "cond-too-far",
            "slug": "btc-too-far",
            "endDate": (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat(),
            "clobTokenIds": '["yes-too-far","no-too-far"]',
            "outcomePrices": "[0.51,0.49]",
            "liquidityNum": 1000,
            "volume24hr": 2000,
            "description": "Resolution text",
        }
    ]
    connector = PolymarketConnector(settings, client=DummyClient([payload]))
    assert connector.discover_active_market() is None


def test_polymarket_connector_prefers_exact_btc_5m_match_score(settings) -> None:
    payload = [
        {
            "id": "vague",
            "question": "Will BTC move in the next few minutes?",
            "conditionId": "cond-vague",
            "slug": "btc-next-minutes",
            "endDate": (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat(),
            "clobTokenIds": '["yes-vague","no-vague"]',
            "outcomePrices": "[0.51,0.49]",
            "liquidityNum": 5000,
            "volume24hr": 6000,
            "description": "Short-term BTC direction market",
        },
        {
            "id": "exact",
            "question": "Will Bitcoin be up or down in 5 minutes?",
            "conditionId": "cond-exact",
            "slug": "btc-5m-exact",
            "endDate": (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat(),
            "clobTokenIds": '["yes-exact","no-exact"]',
            "outcomePrices": "[0.52,0.48]",
            "liquidityNum": 1000,
            "volume24hr": 1500,
            "description": "Resolution text",
        },
    ]
    connector = PolymarketConnector(settings, client=DummyClient([payload]))
    markets = connector.discover_markets()
    assert markets[0].market_id == "exact"


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


def test_polymarket_connector_uses_best_bid_from_unsorted_book(settings) -> None:
    client = DummyClient(
        [
            {
                "bids": [{"price": "0.001", "size": "100"}, {"price": "0.999", "size": "50"}],
                "asks": [],
                "last_trade_price": "0.999",
            }
        ]
    )
    connector = PolymarketConnector(settings, client=client)
    snapshot = connector.get_orderbook_snapshot("yes-token")
    assert snapshot.bid == 0.999
    assert snapshot.ask == 0.0
    assert snapshot.midpoint == 0.999
    assert snapshot.spread == 0.0
    assert snapshot.two_sided is False
    assert snapshot.last_trade_price == 0.999


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


def test_polymarket_connector_probes_live_readiness(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))

    class StubLiveClient:
        def get_address(self):
            return "0xabc"

        def create_or_derive_api_creds(self):
            return {"key": "derived"}

        def set_api_creds(self, creds):
            self.creds = creds

        def get_ok(self):
            return True

        def get_collateral_address(self):
            return "0x2791"

        def get_balance_allowance(self, params):
            assert params.asset_type == "COLLATERAL"
            assert params.signature_type == configured.polymarket_signature_type
            return {"balance": "123450000", "allowance": "98.76"}

        def get_orders(self, params):
            return [
                {"market": "market-1"},
                {"market_id": "market-2"},
                {"market": "market-1"},
            ]

    connector.build_live_client = lambda: StubLiveClient()
    auth = connector.probe_live_readiness()
    assert auth.probe_attempted
    assert auth.wallet_address == "0xabc"
    assert auth.api_credentials_derived
    assert auth.server_ok
    assert auth.readonly_ready
    assert auth.collateral_address == "0x2791"
    assert auth.balance == 123.45
    assert auth.allowance == 98.76
    assert auth.open_orders_count == 3
    assert auth.open_orders_markets == ["market-1", "market-2"]
    assert auth.diagnostics_collected
    assert auth.errors == []


def test_polymarket_connector_captures_probe_errors(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))
    connector.build_live_client = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    auth = connector.probe_live_readiness()
    assert auth.probe_attempted
    assert not auth.readonly_ready
    assert auth.errors == ["boom"]


def test_polymarket_connector_collects_partial_diagnostics_with_errors(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))

    class StubLiveClient:
        def get_address(self):
            return "0xabc"

        def create_or_derive_api_creds(self):
            return {"key": "derived"}

        def set_api_creds(self, creds):
            self.creds = creds

        def get_ok(self):
            return True

        def get_collateral_address(self):
            raise RuntimeError("no collateral")

        def get_balance_allowance(self, params):
            return {"balance": {"available": "50000000"}, "allowance": {"allowance": "40"}}

        def get_orders(self, params):
            raise RuntimeError("orders unavailable")

    connector.build_live_client = lambda: StubLiveClient()
    auth = connector.probe_live_readiness()
    assert auth.readonly_ready
    assert auth.balance == 50.0
    assert auth.allowance == 40.0
    assert auth.open_orders_count == 0
    assert auth.diagnostics_collected
    assert "collateral_address: no collateral" in auth.errors
    assert "open_orders: orders unavailable" in auth.errors


def test_polymarket_connector_discovers_btc_daily_threshold_markets(settings) -> None:
    configured = settings.model_copy(update={"market_family": "btc_daily_threshold"})
    payload = [
        {
            "id": "daily",
            "question": "Will the price of Bitcoin be above $82,000 on April 17?",
            "conditionId": "cond-daily",
            "slug": "bitcoin-above-82k-on-april-17",
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
            "clobTokenIds": '["yes-daily","no-daily"]',
            "outcomePrices": "[0.55,0.45]",
            "liquidityNum": 1000,
            "volume24hr": 5000,
            "description": "This market will resolve to yes if BTC is above the threshold on April 17.",
        },
        {
            "id": "monthly",
            "question": "Will Bitcoin reach $90,000 in April?",
            "conditionId": "cond-monthly",
            "slug": "will-bitcoin-reach-90k-in-april-2026",
            "endDate": (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
            "clobTokenIds": '["yes-monthly","no-monthly"]',
            "outcomePrices": "[0.40,0.60]",
            "liquidityNum": 4000,
            "volume24hr": 7000,
            "description": "This market will immediately resolve if BTC reaches the level during the month.",
        },
    ]
    connector = PolymarketConnector(configured, client=DummyClient([payload]))
    markets = connector.discover_markets()
    assert len(markets) == 1
    assert markets[0].market_id == "daily"


def test_polymarket_connector_prefers_active_btc_daily_threshold_market(settings) -> None:
    configured = settings.model_copy(update={"market_family": "btc_daily_threshold"})
    payload = [
        {
            "id": "near-daily",
            "question": "Will the price of Bitcoin be above $82,000 on April 17?",
            "conditionId": "cond-near",
            "slug": "bitcoin-above-82k-on-april-17",
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
            "clobTokenIds": '["yes-near","no-near"]',
            "outcomePrices": "[0.55,0.45]",
            "liquidityNum": 1000,
            "volume24hr": 5000,
            "description": "Daily threshold market.",
        },
        {
            "id": "far-daily",
            "question": "Will the price of Bitcoin be above $72,000 on April 18?",
            "conditionId": "cond-far",
            "slug": "bitcoin-above-72k-on-april-18",
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat(),
            "clobTokenIds": '["yes-far","no-far"]',
            "outcomePrices": "[0.51,0.49]",
            "liquidityNum": 5000,
            "volume24hr": 9000,
            "description": "Daily threshold market.",
        },
    ]
    connector = PolymarketConnector(configured, client=DummyClient([payload]))
    market = connector.discover_active_market()
    assert market is not None
    assert market.market_id == "near-daily"
