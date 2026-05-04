from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trading_engine.connectors.external_feeds import ExternalFeedConnector
from polymarket_trading_engine.connectors.polymarket import PolymarketConnector
from polymarket_trading_engine.types import DecisionStatus, SuggestedSide, TradeDecision


class DummyResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class DummyClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        if not self.payloads:
            return DummyResponse(None, status_code=404)
        payload = self.payloads.pop(0)
        if isinstance(payload, DummyResponse):
            return payload
        return DummyResponse(payload)


def _event_response(market_dict: dict) -> dict:
    """Wrap a market dict as an /events/slug/<slug> response."""
    return {"markets": [market_dict]}


def test_external_feed_connector_returns_btc_price() -> None:
    client = DummyClient([{"price": "123456.78"}])
    connector = ExternalFeedConnector(client=client)
    assert connector.get_btc_price() == 123456.78


def _make_market_dict(market_id: str, question: str, slug: str, end_minutes_from_now: int = 5,
                      yes_price: float = 0.55) -> dict:
    return {
        "id": market_id,
        "question": question,
        "conditionId": f"cond-{market_id}",
        "slug": slug,
        "endDate": (datetime.now(timezone.utc) + timedelta(minutes=end_minutes_from_now)).isoformat(),
        "clobTokenIds": f'["yes-{market_id}","no-{market_id}"]',
        "outcomePrices": f"[{yes_price},{1.0 - yes_price}]",
        "liquidityNum": 1000,
        "volume24hr": 5000,
        "description": "Resolution text",
    }


def test_predicted_slug_btc_5m_format() -> None:
    # 5m slug is btc-updown-5m-<unix_ts> where ts is current 5-min window start
    slug = PolymarketConnector._predicted_slug("btc_5m", 0)
    assert slug is not None and slug.startswith("btc-updown-5m-")
    ts = int(slug.rsplit("-", 1)[1])
    assert ts % 300 == 0  # aligned to 5-min boundary


def test_predicted_slug_btc_15m_format() -> None:
    slug = PolymarketConnector._predicted_slug("btc_15m", 0)
    assert slug is not None and slug.startswith("btc-updown-15m-")
    ts = int(slug.rsplit("-", 1)[1])
    assert ts % 900 == 0  # aligned to 15-min boundary


def test_predicted_slug_btc_1h_et_format() -> None:
    # Format: bitcoin-up-or-down-{month}-{day}-{year}-{hr}{am/pm}-et
    slug = PolymarketConnector._predicted_slug("btc_1h", 0)
    assert slug is not None
    assert slug.startswith("bitcoin-up-or-down-")
    assert slug.endswith("-et")
    # one of am/pm must appear exactly before -et
    assert ("am-et" in slug) or ("pm-et" in slug)


def test_predicted_slug_unknown_family_returns_none() -> None:
    assert PolymarketConnector._predicted_slug("not-a-family", 0) is None


def test_slug_prediction_discovers_btc_5m_markets(settings) -> None:
    # 3 lookahead slugs → 3 event responses, each wrapping one market
    payloads = [
        _event_response(_make_market_dict("m1", "Bitcoin Up or Down - 10:25AM-10:30AM ET", "btc-updown-5m-aaa", yes_price=0.50)),
        _event_response(_make_market_dict("m2", "Bitcoin Up or Down - 10:30AM-10:35AM ET", "btc-updown-5m-bbb", yes_price=0.55)),
        _event_response(_make_market_dict("m3", "Bitcoin Up or Down - 10:35AM-10:40AM ET", "btc-updown-5m-ccc", yes_price=0.60)),
    ]
    connector = PolymarketConnector(settings, client=DummyClient(payloads))
    markets = connector.discover_markets()
    assert [m.market_id for m in markets] == ["m1", "m2", "m3"]


def test_slug_prediction_tolerates_404s(settings) -> None:
    # First slug resolved (200), next two missing (404). Should return only the first.
    payloads = [
        _event_response(_make_market_dict("m1", "Bitcoin Up or Down - 10:25AM-10:30AM ET", "btc-updown-5m-aaa")),
        DummyResponse(None, status_code=404),
        DummyResponse(None, status_code=404),
    ]
    connector = PolymarketConnector(settings, client=DummyClient(payloads))
    markets = connector.discover_markets()
    assert [m.market_id for m in markets] == ["m1"]


def test_slug_prediction_dedupes_same_market_across_windows(settings) -> None:
    # If Polymarket returns the same market for two consecutive slug lookups (shouldn't
    # happen in practice), we dedupe by market_id.
    same = _make_market_dict("m1", "Bitcoin Up or Down - 10:25AM-10:30AM ET", "btc-updown-5m-aaa")
    payloads = [_event_response(same), _event_response(same), _event_response(same)]
    connector = PolymarketConnector(settings, client=DummyClient(payloads))
    markets = connector.discover_markets()
    assert [m.market_id for m in markets] == ["m1"]


def test_btc_5m_match_score_requires_slug_prefix() -> None:
    # Even with "up or down" + "bitcoin" + "5 minutes", a non-btc-updown-5m slug is rejected.
    score = PolymarketConnector._btc_5m_match_score(
        question="Will Bitcoin be up or down in 5 minutes?",
        description="BTC directional market",
        slug="bitcoin-5-minute-up-down",
    )
    assert score == 0
    # With the canonical slug prefix, it passes.
    good = PolymarketConnector._btc_5m_match_score(
        question="Bitcoin Up or Down - 10:25AM-10:30AM ET",
        description="",
        slug="btc-updown-5m-1776522300",
    )
    assert good >= 3


def test_btc_1h_match_score_requires_bitcoin_updown_et_slug() -> None:
    # Decoy: "Bitcoin Up or Down on April 18?" (daily, not a rolling 1h market)
    decoy = PolymarketConnector._btc_1h_match_score(
        question="Bitcoin Up or Down on April 18?",
        description="",
        slug="bitcoin-up-or-down-april-18",  # missing -<hr>{am/pm}-et
    )
    assert decoy == 0
    # Real rolling 1h market
    good = PolymarketConnector._btc_1h_match_score(
        question="Bitcoin Up or Down - April 18, 10AM ET",
        description="",
        slug="bitcoin-up-or-down-april-18-2026-10am-et",
    )
    assert good >= 3


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


def test_polymarket_connector_executes_live_trade_when_enabled(settings) -> None:
    configured = settings.model_copy(
        update={
            "live_trading_enabled": True,
            "live_order_type": "FOK",
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))

    class StubLiveClient:
        def create_or_derive_api_creds(self):
            return {"key": "derived"}

        def set_api_creds(self, creds):
            self.creds = creds

        def create_order(self, order_args):
            self.order_args = order_args
            return {"signed": True}

        def post_order(self, order, orderType, post_only):
            assert order == {"signed": True}
            assert orderType == "FOK"
            assert post_only is False
            return {"orderID": "live-123", "status": "LIVE_SUBMITTED"}

    stub = StubLiveClient()
    connector.build_live_client = lambda: stub
    decision = TradeDecision(
        market_id="market-1",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.5,
        rationale=["trade"],
        rejected_by=[],
        asset_id="token-yes",
    )
    result = connector.execute_live_trade(decision)
    assert result.success
    assert result.order_id == "live-123"
    assert stub.order_args.token_id == "token-yes"
    assert stub.order_args.price == 0.5
    assert stub.order_args.size == 20.0
    assert stub.order_args.side == "BUY"


def test_polymarket_connector_blocks_live_trade_when_flag_disabled(settings) -> None:
    connector = PolymarketConnector(settings, client=DummyClient([]))
    decision = TradeDecision(
        market_id="market-1",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.5,
        rationale=["trade"],
        rejected_by=[],
        asset_id="token-yes",
    )
    result = connector.execute_live_trade(decision)
    assert not result.success
    assert result.status == "LIVE_DISABLED"


def test_polymarket_connector_lists_live_orders(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))

    class StubLiveClient:
        def create_or_derive_api_creds(self):
            return {"key": "derived"}

        def set_api_creds(self, creds):
            self.creds = creds

        def get_orders(self, params):
            return [
                {"id": "live-1", "market": "m1", "asset_id": "a1", "status": "OPEN", "side": "BUY", "price": "0.5"},
            ]

    connector.build_live_client = lambda: StubLiveClient()
    orders = connector.list_live_orders()
    assert orders[0]["order_id"] == "live-1"
    assert orders[0]["market_id"] == "m1"
    assert orders[0]["price"] == 0.5


def test_polymarket_connector_gets_single_live_order(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))

    class StubLiveClient:
        def create_or_derive_api_creds(self):
            return {"key": "derived"}

        def set_api_creds(self, creds):
            self.creds = creds

        def get_order(self, order_id):
            return {"orderID": order_id, "market_id": "m1", "status": "MATCHED", "size": "20"}

    connector.build_live_client = lambda: StubLiveClient()
    order = connector.get_live_order("live-1")
    assert order["order_id"] == "live-1"
    assert order["status"] == "MATCHED"
    assert order["size"] == 20.0


def test_polymarket_connector_cancels_live_order(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))

    class StubLiveClient:
        def create_or_derive_api_creds(self):
            return {"key": "derived"}

        def set_api_creds(self, creds):
            self.creds = creds

        def cancel_orders(self, order_ids):
            assert order_ids == ["live-1"]
            return {"canceled": ["live-1"]}

    connector.build_live_client = lambda: StubLiveClient()
    result = connector.cancel_live_order("live-1")
    assert result["order_id"] == "live-1"
    assert result["success"] is True


def test_polymarket_connector_lists_live_trades(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))

    class StubLiveClient:
        def create_or_derive_api_creds(self):
            return {"key": "derived"}

        def set_api_creds(self, creds):
            self.creds = creds

        def get_trades(self, params):
            return [
                {"id": "trade-1", "order_id": "live-1", "market": "m1", "price": "0.51", "size": "20"},
            ]

    connector.build_live_client = lambda: StubLiveClient()
    trades = connector.list_live_trades(limit=5)
    assert trades[0]["trade_id"] == "trade-1"
    assert trades[0]["price"] == 0.51
    assert trades[0]["size"] == 20.0


def test_polymarket_connector_gets_single_live_trade(settings) -> None:
    configured = settings.model_copy(
        update={
            "polymarket_private_key": "0x" + "1" * 64,
        }
    )
    connector = PolymarketConnector(configured, client=DummyClient([]))
    connector.list_live_trades = lambda market_id=None, limit=100: [
        {"trade_id": "trade-1", "order_id": "live-1"},
        {"trade_id": "trade-2", "order_id": "live-2"},
    ]
    trade = connector.get_live_trade("trade-2")
    assert trade["order_id"] == "live-2"


def test_polymarket_connector_lists_public_market_trades(settings) -> None:
    payload = [
        {
            "id": "trade-public-1",
            "conditionId": "cond-123",
            "asset": "yes-token",
            "side": "BUY",
            "outcome": "Yes",
            "price": 0.61,
            "size": 42.5,
            "timestamp": 1776452750,
            "title": "Will BTC be above 82k?",
            "slug": "btc-above-82k",
        }
    ]
    connector = PolymarketConnector(settings, client=DummyClient([payload]))
    trades = connector.list_market_trades("cond-123", limit=5)
    assert trades[0]["trade_id"] == "trade-public-1"
    assert trades[0]["market_id"] == "cond-123"
    assert trades[0]["outcome"] == "Yes"
    assert trades[0]["price"] == 0.61


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
