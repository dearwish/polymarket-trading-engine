from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from polymarket_trading_engine.engine.risk import RiskEngine
from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.service import AgentService
from polymarket_trading_engine.types import (
    AuthStatus,
    DecisionStatus,
    ExecutionMode,
    ExecutionResult,
    ExecutionStyle,
    MarketCandidate,
    MarketSnapshot,
    OrderBookSnapshot,
    OrderSide,
    PositionRecord,
    SuggestedSide,
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        openrouter_api_key="",
        polymarket_private_key="0x",
        polymarket_funder="",
        polymarket_signature_type=0,
        trading_mode="live",
        live_trading_enabled=True,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
    )
    base.update(overrides)
    return Settings(**base)


def test_risk_build_close_decision_targets_opposite_side(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = RiskEngine(settings)
    position = PositionRecord(
        market_id="m1",
        side=SuggestedSide.YES,
        size_usd=10.0,
        entry_price=0.50,
        order_id="live-1",
    )
    candidate = MarketCandidate(
        market_id="m1",
        question="Bitcoin up or down in 1h",
        condition_id="cond-1",
        slug="btc-1h",
        end_date_iso="2099-01-01T00:00:00Z",
        yes_token_id="token-yes",
        no_token_id="token-no",
        implied_probability=0.52,
        liquidity_usd=1000.0,
        volume_24h_usd=500.0,
    )
    snapshot = MarketSnapshot(
        candidate=candidate,
        orderbook=OrderBookSnapshot(
            bid=0.51, ask=0.53, midpoint=0.52, spread=0.02, depth_usd=100.0, last_trade_price=0.52,
        ),
        seconds_to_expiry=900,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        external_price=70000.0,
    )
    decision = engine.build_close_decision(position, snapshot)
    assert decision.status == DecisionStatus.APPROVED
    assert decision.order_side == OrderSide.SELL
    assert decision.intent == "CLOSE"
    assert decision.asset_id == "token-yes"
    assert decision.size_usd == 10.0
    assert decision.limit_price == 0.51  # YES close hits the bid


def test_risk_build_close_decision_for_no_position_hits_ask(tmp_path: Path) -> None:
    engine = RiskEngine(_settings(tmp_path))
    position = PositionRecord(
        market_id="m1",
        side=SuggestedSide.NO,
        size_usd=5.0,
        entry_price=0.40,
        order_id="live-2",
    )
    candidate = MarketCandidate(
        market_id="m1",
        question="Bitcoin up or down in 1h",
        condition_id="cond-1",
        slug="btc-1h",
        end_date_iso="2099-01-01T00:00:00Z",
        yes_token_id="token-yes",
        no_token_id="token-no",
        implied_probability=0.5,
        liquidity_usd=1000.0,
        volume_24h_usd=500.0,
    )
    snapshot = MarketSnapshot(
        candidate=candidate,
        orderbook=OrderBookSnapshot(
            bid=0.48, ask=0.52, midpoint=0.50, spread=0.04, depth_usd=100.0, last_trade_price=0.50,
        ),
        seconds_to_expiry=600,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        external_price=70000.0,
    )
    decision = engine.build_close_decision(position, snapshot)
    assert decision.asset_id == "token-no"
    assert decision.order_side == OrderSide.SELL
    assert decision.limit_price == 0.52


def test_service_close_live_position_posts_counter_order(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    # Seed an open YES position.
    service.portfolio.record_live_fill(
        order_id="live-1",
        market_id="m1",
        asset_id="token-yes",
        side=SuggestedSide.YES,
        fill_price=0.50,
        filled_size_shares=20.0,
    )

    captured: dict[str, object] = {}

    def fake_build_snapshot(market_id: str) -> MarketSnapshot:
        return MarketSnapshot(
            candidate=MarketCandidate(
                market_id="m1",
                question="Bitcoin up or down",
                condition_id="cond-1",
                slug="btc-1h",
                end_date_iso="2099-01-01T00:00:00Z",
                yes_token_id="token-yes",
                no_token_id="token-no",
                implied_probability=0.55,
                liquidity_usd=1000.0,
                volume_24h_usd=500.0,
            ),
            orderbook=OrderBookSnapshot(
                bid=0.55, ask=0.57, midpoint=0.56, spread=0.02, depth_usd=500.0, last_trade_price=0.56,
                bid_levels=[(0.55, 100.0)], ask_levels=[(0.57, 100.0)],
            ),
            seconds_to_expiry=900,
            recent_price_change_bps=0.0,
            recent_trade_count=0,
            external_price=70000.0,
        )

    service.build_market_snapshot = fake_build_snapshot  # type: ignore[assignment]

    service.polymarket.probe_live_readiness = lambda: AuthStatus(  # type: ignore[assignment]
        private_key_configured=True,
        funder_configured=True,
        signature_type=0,
        live_client_constructible=True,
        missing=[],
        readonly_ready=True,
    )

    def fake_execute(decision, orderbook, **kwargs) -> ExecutionResult:
        captured["decision"] = decision
        captured["tte"] = kwargs.get("seconds_to_expiry")
        return ExecutionResult(
            market_id=decision.market_id,
            success=True,
            mode=ExecutionMode.LIVE,
            order_id="live-close-1",
            status="FILLED",
            detail="closed",
            fill_price=0.55,
            filled_size_shares=20.0,
            order_side=OrderSide.SELL,
            asset_id=decision.asset_id,
            execution_style=ExecutionStyle.FOK_TAKER,
        )

    service.execution.execute_trade = fake_execute  # type: ignore[assignment]

    action = service.close_position("m1", reason="manual_close")
    assert action.action == "CLOSE"
    decision = captured["decision"]
    assert decision.order_side == OrderSide.SELL  # type: ignore[attr-defined]
    assert decision.intent == "CLOSE"  # type: ignore[attr-defined]
    assert captured["tte"] == 900
    # Position is now closed with the live fill price.
    assert service.portfolio.list_open_positions() == []
    closed = service.portfolio.list_closed_positions()
    assert closed[0].exit_price == 0.55


def test_service_close_live_position_raises_without_auth(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    service.portfolio.record_live_fill(
        order_id="live-2",
        market_id="m1",
        asset_id="token-yes",
        side=SuggestedSide.YES,
        fill_price=0.50,
        filled_size_shares=10.0,
    )
    service.build_market_snapshot = lambda market_id: MarketSnapshot(  # type: ignore[assignment]
        candidate=MarketCandidate(
            market_id="m1",
            question="Bitcoin up or down",
            condition_id="c",
            slug="s",
            end_date_iso="2099-01-01T00:00:00Z",
            yes_token_id="token-yes",
            no_token_id="token-no",
            implied_probability=0.5,
            liquidity_usd=1000.0,
            volume_24h_usd=500.0,
        ),
        orderbook=OrderBookSnapshot(bid=0.5, ask=0.52, midpoint=0.51, spread=0.02, depth_usd=100.0, last_trade_price=0.51),
        seconds_to_expiry=600,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        external_price=70000.0,
    )
    service.polymarket.probe_live_readiness = lambda: AuthStatus(  # type: ignore[assignment]
        private_key_configured=False,
        funder_configured=False,
        signature_type=0,
        live_client_constructible=False,
        missing=["polymarket_private_key"],
        readonly_ready=False,
    )
    import pytest
    with pytest.raises(RuntimeError, match="readonly-ready"):
        service.close_position("m1")
