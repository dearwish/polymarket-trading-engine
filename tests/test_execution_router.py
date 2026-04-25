from __future__ import annotations

from pathlib import Path

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.engine.execution import ExecutionEngine, ExecutionRouter
from polymarket_ai_agent.types import (
    DecisionStatus,
    ExecutionMode,
    ExecutionStyle,
    OrderBookSnapshot,
    OrderSide,
    SuggestedSide,
    TradeDecision,
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        openrouter_api_key="",
        polymarket_private_key="",
        polymarket_funder="",
        polymarket_signature_type=0,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
    )
    base.update(overrides)
    return Settings(**base)


def _approved_decision(side: SuggestedSide = SuggestedSide.YES) -> TradeDecision:
    return TradeDecision(
        market_id="m1",
        status=DecisionStatus.APPROVED,
        side=side,
        size_usd=10.0,
        limit_price=0.55,
        rationale=["go"],
        rejected_by=[],
        asset_id="token-yes",
        order_side=OrderSide.BUY,
    )


def _orderbook(
    bid: float = 0.48,
    ask: float = 0.52,
    bid_levels: list[tuple[float, float]] | None = None,
    ask_levels: list[tuple[float, float]] | None = None,
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        bid=bid,
        ask=ask,
        midpoint=round((bid + ask) / 2, 4),
        spread=round(ask - bid, 4),
        depth_usd=500.0,
        last_trade_price=round((bid + ask) / 2, 4),
        two_sided=bid > 0 and ask > 0,
        bid_levels=bid_levels or [(bid, 100.0), (bid - 0.01, 50.0)],
        ask_levels=ask_levels or [(ask, 80.0), (ask + 0.01, 60.0)],
    )


def test_router_chooses_maker_when_edge_large_and_tte_far(tmp_path: Path) -> None:
    router = ExecutionRouter(_settings(tmp_path, execution_maker_min_edge=0.04, execution_maker_min_tte_seconds=60))
    routed = router.route(_approved_decision(), _orderbook(), seconds_to_expiry=900, edge=0.08)
    assert routed.style == ExecutionStyle.GTC_MAKER
    assert routed.post_only is True
    assert routed.limit_price == 0.48  # joins the bid


def test_router_falls_back_to_taker_when_edge_too_small(tmp_path: Path) -> None:
    router = ExecutionRouter(_settings(tmp_path, execution_maker_min_edge=0.05))
    routed = router.route(_approved_decision(), _orderbook(), seconds_to_expiry=900, edge=0.02)
    assert routed.style == ExecutionStyle.FOK_TAKER
    assert routed.post_only is False
    assert routed.limit_price == 0.52
    assert "edge" in routed.reason


def test_router_falls_back_to_taker_when_tte_too_short(tmp_path: Path) -> None:
    router = ExecutionRouter(_settings(tmp_path, execution_maker_min_tte_seconds=300))
    routed = router.route(_approved_decision(), _orderbook(), seconds_to_expiry=60, edge=0.10)
    assert routed.style == ExecutionStyle.FOK_TAKER
    assert "tte" in routed.reason


def test_router_skips_maker_when_book_not_two_sided(tmp_path: Path) -> None:
    router = ExecutionRouter(_settings(tmp_path))
    routed = router.route(_approved_decision(), _orderbook(bid=0.0, ask=0.55), seconds_to_expiry=900, edge=0.10)
    assert routed.style == ExecutionStyle.FOK_TAKER
    assert routed.reason == "book_not_two_sided"


def test_router_taker_sell_prices_to_bid(tmp_path: Path) -> None:
    router = ExecutionRouter(_settings(tmp_path, execution_maker_min_edge=10.0))  # force taker
    decision = _approved_decision()
    decision.order_side = OrderSide.SELL
    routed = router.route(decision, _orderbook(), seconds_to_expiry=900, edge=0.01)
    assert routed.style == ExecutionStyle.FOK_TAKER
    assert routed.limit_price == 0.48  # bid


def test_router_should_replace_triggers_when_best_moves_more_than_threshold(tmp_path: Path) -> None:
    # Default min_ticks=2.0 → threshold is 2 ticks; sub-2-tick wiggles are
    # absorbed (no churn) but a real best-level shift triggers the replace.
    router = ExecutionRouter(_settings(tmp_path, execution_price_tick=0.01))
    decision = _approved_decision()
    orderbook = _orderbook(bid=0.50)
    assert router.should_replace(existing_limit_price=0.45, orderbook=orderbook, decision=decision) is True
    # 1-tick drift is absorbed by the new 2-tick hysteresis floor.
    assert router.should_replace(existing_limit_price=0.49, orderbook=orderbook, decision=decision) is False
    # Sub-tick noise is also absorbed.
    assert router.should_replace(existing_limit_price=0.495, orderbook=orderbook, decision=decision) is False


def test_router_should_replace_threshold_configurable(tmp_path: Path) -> None:
    # min_ticks=1.0 reproduces the legacy "strictly more than one tick"
    # behaviour for any operator who wants a tighter cancel/replace cadence.
    router = ExecutionRouter(_settings(tmp_path, execution_price_tick=0.01, execution_replace_min_ticks=1.0))
    decision = _approved_decision()
    orderbook = _orderbook(bid=0.50)
    # ~half a tick apart → no replace.
    assert router.should_replace(existing_limit_price=0.495, orderbook=orderbook, decision=decision) is False
    # ~1.5 ticks apart → fires.
    assert router.should_replace(existing_limit_price=0.485, orderbook=orderbook, decision=decision) is True


def test_router_should_replace_size_drift_triggers_when_above_pct(tmp_path: Path) -> None:
    # Price within hysteresis but resting size has drifted too far from target.
    router = ExecutionRouter(_settings(tmp_path, execution_price_tick=0.01, execution_replace_min_size_pct=0.10))
    decision = _approved_decision()
    orderbook = _orderbook(bid=0.50)
    # Price stable (fresh = existing = 0.50). 8% size drift → no replace.
    assert (
        router.should_replace(
            existing_limit_price=0.50,
            orderbook=orderbook,
            decision=decision,
            existing_size=100.0,
            target_size=108.0,
        )
        is False
    )
    # 15% drift → replace.
    assert (
        router.should_replace(
            existing_limit_price=0.50,
            orderbook=orderbook,
            decision=decision,
            existing_size=100.0,
            target_size=85.0,
        )
        is True
    )


def test_execution_engine_paper_vwap_walks_ask_levels(tmp_path: Path) -> None:
    engine = ExecutionEngine(ExecutionMode.PAPER, paper_entry_slippage_bps=0.0)
    decision = _approved_decision()
    # size_usd=10 at limit 0.55 ⇒ target ~18.18 shares; ask levels [(0.50, 10), (0.51, 20)]
    orderbook = _orderbook(ask_levels=[(0.50, 10.0), (0.51, 20.0)])
    result = engine.execute_trade(decision, orderbook, seconds_to_expiry=600, edge=0.0)
    assert result.success
    assert result.status == "FILLED_PAPER"
    assert result.filled_size_shares > 10.0  # consumed first level entirely
    # VWAP between 0.50 and 0.51 should fall inside the spread.
    assert 0.50 <= result.fill_price <= 0.51


def test_execution_engine_paper_sell_walks_bid_levels(tmp_path: Path) -> None:
    engine = ExecutionEngine(ExecutionMode.PAPER, paper_entry_slippage_bps=0.0)
    decision = _approved_decision()
    decision.order_side = OrderSide.SELL
    orderbook = _orderbook(bid_levels=[(0.48, 100.0)])
    result = engine.execute_trade(decision, orderbook, seconds_to_expiry=600, edge=0.0)
    assert result.success
    assert result.order_side == OrderSide.SELL
    assert result.fill_price == 0.48


def test_execution_engine_falls_back_to_constant_slippage_without_levels(tmp_path: Path) -> None:
    engine = ExecutionEngine(ExecutionMode.PAPER, paper_entry_slippage_bps=10.0)
    orderbook = OrderBookSnapshot(
        bid=0.51, ask=0.52, midpoint=0.515, spread=0.01, depth_usd=100.0, last_trade_price=0.515,
    )
    result = engine.execute_trade(_approved_decision(), orderbook, seconds_to_expiry=60, edge=0.0)
    assert result.success
    # Constant-slippage path: ask * (1 + 10bps).
    assert result.fill_price > 0.52


def test_execution_engine_router_flips_decision_to_maker(tmp_path: Path) -> None:
    router = ExecutionRouter(_settings(tmp_path, execution_maker_min_edge=0.04, execution_maker_min_tte_seconds=60))
    engine = ExecutionEngine(ExecutionMode.PAPER, router=router, settings=_settings(tmp_path))
    result = engine.execute_trade(_approved_decision(), _orderbook(), seconds_to_expiry=900, edge=0.08)
    assert result.execution_style == ExecutionStyle.GTC_MAKER
