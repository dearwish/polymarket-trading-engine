from __future__ import annotations

import json

import httpx

from polymarket_trading_engine.connectors.binance_ws import BinanceBtcFeed


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


def test_aggtrade_parses_quantity() -> None:
    envelope = {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade", "s": "BTCUSDT",
            "p": "70000.00", "q": "0.12345", "T": 1_700_000_000_000,
        },
    }
    tick = BinanceBtcFeed.parse_message(json.dumps(envelope))
    assert tick is not None
    assert tick.quantity == 0.12345


def test_bookticker_has_zero_quantity() -> None:
    envelope = {
        "stream": "btcusdt@bookTicker",
        "data": {"u": 1, "s": "BTCUSDT", "b": "70000.00", "a": "70010.00"},
    }
    tick = BinanceBtcFeed.parse_message(json.dumps(envelope))
    assert tick is not None
    assert tick.quantity == 0.0  # non-trade payload, no qty signal


def test_rest_klines_parses_binance_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["interval"] == "1m"
        assert request.url.params["limit"] == "3"
        # Minimal shape: [open_time_ms, open, high, low, close, volume, close_time_ms, ...]
        return httpx.Response(200, json=[
            [1_700_000_000_000, "70000", "70050", "69950", "70010.50", "12.34", 1_700_000_059_999],
            [1_700_000_060_000, "70010", "70060", "69990", "70020.00", "8.10",  1_700_000_119_999],
            [1_700_000_120_000, "70020", "70080", "70005", "70055.25", "15.00", 1_700_000_179_999],
        ])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = BinanceBtcFeed(symbol="btcusdt", http_client=client)
    bars = feed.rest_klines("1m", 3)
    assert len(bars) == 3
    ts0, close0, vol0 = bars[0]
    assert close0 == 70010.50
    assert vol0 == 12.34
    assert ts0.tzinfo is not None


def test_rest_klines_returns_empty_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = BinanceBtcFeed(http_client=client)
    assert feed.rest_klines() == []


def test_rest_klines_drops_malformed_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            [1_700_000_000_000, "70000", "70050", "69950", "70010", "1.0", 0],  # ok
            "not-a-list",                                                         # skip
            [1_700_000_060_000, "70010", "70060", "69990", "not-a-number", "1.0", 0],  # skip
            [1_700_000_120_000, "70020", "70080", "70005", "0", "1.0", 0],       # skip (close<=0)
            [1_700_000_180_000, "70020", "70080", "70005", "70020", "-1.0", 0],  # skip (neg vol)
            [1_700_000_240_000, "70020", "70080", "70005", "70030", "1.0", 0],   # ok
        ])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = BinanceBtcFeed(http_client=client)
    bars = feed.rest_klines()
    assert len(bars) == 2
    assert [b[1] for b in bars] == [70010.0, 70030.0]


def test_rest_klines_paginates_when_limit_exceeds_binance_cap() -> None:
    """Binance /klines returns max 1000 rows per call; asking for 1440 must
    page back via endTime and merge into one contiguous list."""
    call_log: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        call_log.append(params)
        limit = int(params["limit"])
        end_time = int(params.get("endTime") or 1_700_000_000_000 + 1440 * 60_000)
        # Build `limit` bars ending at end_time, each 60s apart.
        rows = []
        for i in range(limit):
            open_time = end_time - (limit - 1 - i) * 60_000
            rows.append([open_time, "70000", "70050", "69950", f"{70010 + i}", "1.0", open_time + 59_999])
        return httpx.Response(200, json=rows)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = BinanceBtcFeed(symbol="btcusdt", http_client=client)
    bars = feed.rest_klines("1m", 1440)
    # Two calls: first 1000 (most recent), then 440 older.
    assert len(call_log) == 2
    assert int(call_log[0]["limit"]) == 1000
    assert "endTime" not in call_log[0]
    assert int(call_log[1]["limit"]) == 440
    assert "endTime" in call_log[1]
    # Result must be chronological (ascending timestamps) and 1440 long.
    assert len(bars) == 1440
    times = [b[0] for b in bars]
    assert times == sorted(times)


def test_rest_klines_stops_when_source_exhausted() -> None:
    """If a page returns fewer rows than requested, pagination must stop."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # Return only 5 rows despite limit=1000.
            return httpx.Response(200, json=[
                [1_700_000_000_000 + i * 60_000, "70000", "70050", "69950", f"{70010 + i}", "1.0", 0]
                for i in range(5)
            ])
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = BinanceBtcFeed(http_client=client)
    bars = feed.rest_klines("1m", 1440)
    assert len(bars) == 5
    assert calls["n"] == 1
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "oops"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = BinanceBtcFeed(http_client=client)
    assert feed.rest_klines() == []
