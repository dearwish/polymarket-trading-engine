from polymarket_ai_agent.connectors.polymarket_ws import PolymarketMarketStream, PolymarketUserStream


def test_market_stream_parses_event_message() -> None:
    event = PolymarketMarketStream.parse_message(
        '{"event_type":"price_change","market":"cond-1","price_changes":[{"asset_id":"yes-token","price":"0.5"}]}'
    )
    assert event is not None
    assert event.event_type == "price_change"
    assert event.payload["market"] == "cond-1"


def test_market_stream_ignores_invalid_message() -> None:
    event = PolymarketMarketStream.parse_message("not-json")
    assert event is None


def test_parse_messages_handles_list_payload() -> None:
    events = PolymarketMarketStream._parse_messages(
        '[{"event_type":"book","asset_id":"a"},{"event_type":"price_change","asset_id":"b"}]'
    )
    assert [e.event_type for e in events] == ["book", "price_change"]


def test_parse_messages_handles_bytes_payload() -> None:
    events = PolymarketMarketStream._parse_messages(b'{"event_type":"book","asset_id":"a"}')
    assert len(events) == 1
    assert events[0].event_type == "book"


def test_parse_messages_ignores_messages_without_event_type() -> None:
    assert PolymarketMarketStream._parse_messages('{"ping": 1}') == []


def test_user_stream_builds_auth_payload() -> None:
    stream = PolymarketUserStream(
        url="wss://example",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        markets=["cond-1"],
    )
    payload = stream._subscription_payload(["yes-token"])
    assert '"type": "user"' in payload
    assert '"apiKey": "key"' in payload
    assert '"secret": "secret"' in payload
    assert '"passphrase": "pass"' in payload
    assert '"markets": ["cond-1"]' in payload
