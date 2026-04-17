from __future__ import annotations

import json

import httpx

from polymarket_ai_agent.connectors.binance_ws import BinanceBtcFeed


def test_stream_url_combines_both_streams() -> None:
    feed = BinanceBtcFeed(ws_url="wss://stream.binance.com:9443/stream", symbol="BTCUSDT")
    url = feed.stream_url()
    assert url.startswith("wss://stream.binance.com:9443/stream?streams=")
    assert "btcusdt@aggTrade" in url
    assert "btcusdt@bookTicker" in url


def test_parse_aggtrade_envelope() -> None:
    envelope = {
        "stream": "btcusdt@aggTrade",
        "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "70123.45", "T": 1_700_000_000_000},
    }
    tick = BinanceBtcFeed.parse_message(json.dumps(envelope))
    assert tick is not None
    assert tick.price == 70123.45
    assert tick.source == "aggTrade"


def test_parse_bookticker_envelope_uses_midprice() -> None:
    envelope = {
        "stream": "btcusdt@bookTicker",
        "data": {"u": 1, "s": "BTCUSDT", "b": "70000.00", "a": "70010.00"},
    }
    tick = BinanceBtcFeed.parse_message(json.dumps(envelope))
    assert tick is not None
    assert tick.price == 70005.0
    assert tick.source == "bookTicker"


def test_parse_returns_none_for_invalid_payload() -> None:
    assert BinanceBtcFeed.parse_message("not-json") is None
    assert BinanceBtcFeed.parse_message(json.dumps({"unrelated": True})) is None


def test_rest_price_uses_injected_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "71111.11"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = BinanceBtcFeed(symbol="btcusdt", http_client=client)
    tick = feed.rest_price()
    assert tick is not None
    assert tick.price == 71111.11
    assert tick.source == "rest"


def test_rest_price_returns_none_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = BinanceBtcFeed(http_client=client)
    assert feed.rest_price() is None
