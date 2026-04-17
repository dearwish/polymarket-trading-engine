from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_ai_agent.engine.market_state import MarketState


def _ts(seconds: float) -> datetime:
    return datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def _book_snapshot(asset_id: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> dict:
    return {
        "event_type": "book",
        "asset_id": asset_id,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


def test_market_state_book_snapshot_updates_features() -> None:
    state = MarketState(market_id="m-1", yes_token_id="yes", no_token_id="no")
    state.apply_book_snapshot(
        _book_snapshot("yes", [("0.48", "100"), ("0.47", "200")], [("0.52", "120"), ("0.53", "80")])
    )
    features = state.features()
    assert features.bid_yes == 0.48
    assert features.ask_yes == 0.52
    assert features.spread_yes == 0.04
    assert features.mid_yes == 0.5
    # Microprice: ask=0.52 with bid_size=100, bid=0.48 with ask_size=120 -> (0.52*100 + 0.48*120)/220
    assert abs(features.microprice_yes - ((0.52 * 100 + 0.48 * 120) / 220)) < 1e-6
    assert features.two_sided is True
    # Depth: top-5 both sides. bids: 0.48*100 + 0.47*200 = 142; asks: 0.52*120 + 0.53*80 = 104.8; total 246.8
    assert abs(features.depth_usd_yes - 246.8) < 1e-6


def test_market_state_price_change_adds_and_removes_levels() -> None:
    state = MarketState(market_id="m-1", yes_token_id="yes", no_token_id="no")
    state.apply_book_snapshot(
        _book_snapshot("yes", [("0.48", "100")], [("0.52", "100")])
    )
    state.apply_price_change(
        {
            "event_type": "price_change",
            "asset_id": "yes",
            "price_changes": [
                {"price": "0.49", "size": "50", "side": "BUY"},
                {"price": "0.52", "size": "0", "side": "SELL"},
                {"price": "0.53", "size": "75", "side": "SELL"},
            ],
        }
    )
    features = state.features()
    assert features.bid_yes == 0.49
    assert features.ask_yes == 0.53
    assert features.spread_yes == 0.04


def test_market_state_price_change_infers_side_without_explicit_side() -> None:
    state = MarketState(market_id="m-1", yes_token_id="yes", no_token_id="no")
    state.apply_book_snapshot(
        _book_snapshot("yes", [("0.48", "100")], [("0.52", "100")])
    )
    # No side: below mid (0.50) should go to bids; above mid to asks.
    state.apply_price_change(
        {
            "event_type": "price_change",
            "asset_id": "yes",
            "price_changes": [
                {"price": "0.495", "size": "20"},
                {"price": "0.515", "size": "30"},
            ],
        }
    )
    features = state.features()
    assert features.bid_yes == 0.495
    assert features.ask_yes == 0.515


def test_market_state_signed_flow_is_positive_when_yes_is_bought() -> None:
    state = MarketState(
        market_id="m-1",
        yes_token_id="yes",
        no_token_id="no",
        signed_flow_window_seconds=5.0,
    )
    state.apply_last_trade(
        {"event_type": "trade", "asset_id": "yes", "price": "0.51", "size": "10", "side": "BUY"}
    )
    state.apply_last_trade(
        {"event_type": "trade", "asset_id": "yes", "price": "0.52", "size": "5", "side": "SELL"}
    )
    flow, count = state.signed_flow(window_seconds=60.0)
    assert count == 2
    # +0.51*10 - 0.52*5 = 5.10 - 2.60 = 2.50
    assert abs(flow - 2.5) < 1e-6


def test_market_state_signed_flow_window_excludes_old_trades() -> None:
    state = MarketState(market_id="m-1", yes_token_id="yes", no_token_id="no")
    old_ts = _ts(0.0)
    recent_ts = _ts(3600.0)
    state.trade_tape.append((old_ts, "yes", 0.5, 10.0, "BUY"))
    state.trade_tape.append((recent_ts, "yes", 0.6, 20.0, "SELL"))
    flow, count = state.signed_flow(window_seconds=5.0, now=recent_ts)
    assert count == 1
    assert flow == -0.6 * 20.0


def test_market_state_ignores_unknown_asset_ids() -> None:
    state = MarketState(market_id="m-1", yes_token_id="yes", no_token_id="no")
    state.apply_book_snapshot(_book_snapshot("other", [("0.48", "100")], [("0.52", "100")]))
    features = state.features()
    assert features.bid_yes == 0.0
    assert features.ask_yes == 0.0
    assert features.two_sided is False
