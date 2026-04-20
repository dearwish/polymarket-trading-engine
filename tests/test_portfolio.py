from __future__ import annotations

import sqlite3
from datetime import timedelta

from polymarket_ai_agent.engine.portfolio import PortfolioEngine
from polymarket_ai_agent.types import DecisionStatus, ExecutionMode, ExecutionResult, SuggestedSide, TradeDecision, utc_now


def test_portfolio_records_paper_execution(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.53,
    )
    engine.record_execution(decision, result)
    positions = engine.list_open_positions()
    assert len(positions) == 1
    assert positions[0].market_id == "123"
    assert positions[0].entry_price == 0.53
    account_state = engine.get_account_state(ExecutionMode.PAPER)
    assert account_state.open_positions == 1
    assert account_state.available_usd == settings.paper_starting_balance_usd - 10.0


def test_portfolio_marks_due_positions(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.52,
    )
    engine.record_execution(decision, result)
    due = engine.positions_due_for_close(60, now=utc_now() + timedelta(seconds=61))
    assert len(due) == 1


def test_portfolio_closes_position_and_realizes_pnl(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.50,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.50,
    )
    engine.record_execution(decision, result)
    action = engine.close_position("123", exit_price=0.60, reason="ttl_expired")
    assert action.action == "CLOSE"
    assert engine.list_open_positions() == []
    account_state = engine.get_account_state(ExecutionMode.PAPER)
    assert account_state.daily_realized_pnl > 0
    closed = engine.list_closed_positions(limit=5)
    assert len(closed) == 1
    assert closed[0].close_reason == "ttl_expired"


def test_portfolio_partial_close_splits_position(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="ladder-mkt",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.50,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="ladder-mkt",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-ladder-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.50,
    )
    engine.record_execution(decision, result)
    action = engine.partial_close_position("ladder-mkt", fraction=0.5, exit_price=0.60, reason="paper_tp_ladder_1")
    assert action.action == "PARTIAL_CLOSE"
    open_positions = engine.list_open_positions()
    assert len(open_positions) == 1
    assert abs(open_positions[0].size_usd - 5.0) < 1e-6
    closed = engine.list_closed_positions(limit=5)
    assert len(closed) == 1
    assert closed[0].size_usd == 5.0
    # PnL for the closed 5.0 USD half: (0.60 - 0.50) × (5 / 0.50) = 1.00
    assert abs(closed[0].realized_pnl - 1.0) < 1e-6
    assert closed[0].close_reason == "paper_tp_ladder_1"


def test_portfolio_exit_slippage_is_a_no_op_when_disabled(settings) -> None:
    """exit_slippage_bps=0 must leave the price unchanged (default behaviour)."""
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd, exit_slippage_bps=0.0)
    assert engine.apply_exit_slippage(0.55) == 0.55


def test_portfolio_exit_slippage_nudges_price_down(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd, exit_slippage_bps=10.0)
    # 0.55 × (1 - 10/10000) = 0.54945
    assert abs(engine.apply_exit_slippage(0.55) - 0.54945) < 1e-6


def test_portfolio_max_paper_order_counter_returns_zero_when_empty(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    assert engine.max_paper_order_counter() == 0


def test_portfolio_max_paper_order_counter_reads_highest_existing_id(settings) -> None:
    """Simulates a prior run: insert a few positions with paper-order-NNNNNN
    IDs (including a tranche suffix) and confirm the counter picks up the
    largest N so the next ExecutionEngine won't reuse an ID."""
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    for order_id in ("paper-order-000003", "paper-order-000007-T1000000", "paper-order-000005"):
        decision = TradeDecision(
            market_id=f"m-{order_id}",
            status=DecisionStatus.APPROVED,
            side=SuggestedSide.YES,
            size_usd=10.0,
            limit_price=0.50,
            rationale=["approved"],
            rejected_by=[],
        )
        result = ExecutionResult(
            market_id=decision.market_id,
            success=True, mode=ExecutionMode.PAPER,
            order_id=order_id, status="FILLED_PAPER", detail="ok", fill_price=0.50,
        )
        engine.record_execution(decision, result)
    assert engine.max_paper_order_counter() == 7


def test_portfolio_round_trip_fee_deducted_from_realised_pnl(settings) -> None:
    """With fee_bps > 0, close_position should reduce realised_pnl by
    2 × size_usd × fee_bps / 10000 (buy + sell leg)."""
    engine = PortfolioEngine(
        settings.db_path,
        settings.paper_starting_balance_usd,
        fee_bps=50.0,  # 50bps each leg → 1% round-trip
    )
    decision = TradeDecision(
        market_id="fee-mkt",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.50,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="fee-mkt", success=True, mode=ExecutionMode.PAPER,
        order_id="paper-fee-1", status="FILLED_PAPER", detail="ok", fill_price=0.50,
    )
    engine.record_execution(decision, result)
    engine.close_position("fee-mkt", exit_price=0.60, reason="test")
    closed = engine.list_closed_positions(limit=1)
    # Gross PnL: (0.60 - 0.50) × (10 / 0.50) = $2.00
    # Round-trip fee: 10 × 0.005 × 2 = $0.10
    # Net: $1.90
    assert abs(closed[0].realized_pnl - 1.9) < 1e-6


def test_portfolio_partial_close_at_full_fraction_falls_through_to_full_close(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="full-mkt",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.50,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="full-mkt", success=True, mode=ExecutionMode.PAPER,
        order_id="paper-full-1", status="FILLED_PAPER", detail="ok", fill_price=0.50,
    )
    engine.record_execution(decision, result)
    action = engine.partial_close_position("full-mkt", fraction=1.0, exit_price=0.60, reason="paper_take_profit")
    assert action.action == "CLOSE"
    assert engine.list_open_positions() == []


def test_portfolio_no_side_pnl_sign_is_correct(settings) -> None:
    """Buying NO at 0.52 and closing at 0.80 should be a WIN (+), not a loss.

    Regression test for the pre-fix bug where NO-side PnL used the YES formula
    and flipped the sign.
    """
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="no-mkt",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.NO,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
    )
    # fill_price is now in the NO-token frame (post-fix).
    result = ExecutionResult(
        market_id="no-mkt",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-no-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.52,
    )
    engine.record_execution(decision, result)
    # Close at NO = 0.80 → NO went up, we win.
    engine.close_position("no-mkt", exit_price=0.80, reason="ttl_expired")
    closed = engine.list_closed_positions(limit=1)
    assert closed[0].realized_pnl > 0
    # Roughly (0.80 - 0.52) × (10/0.52) ≈ $5.38
    assert abs(closed[0].realized_pnl - (0.28 * 10 / 0.52)) < 0.01


def test_portfolio_estimates_exit_price_for_yes(settings) -> None:
    from polymarket_ai_agent.types import OrderBookSnapshot, PositionRecord

    orderbook = OrderBookSnapshot(
        bid=0.60,
        ask=0.62,
        midpoint=0.61,
        spread=0.02,
        depth_usd=100.0,
        last_trade_price=0.61,
    )
    position = PositionRecord(market_id="123", side=SuggestedSide.YES, size_usd=10.0, entry_price=0.50)
    price = PortfolioEngine.estimate_exit_price(position, orderbook, exit_slippage_bps=10)
    assert price < 0.60


def test_portfolio_consecutive_losses_counts_trailing_streak(settings) -> None:
    """get_consecutive_losses walks closed positions newest-first and stops
    at the first non-losing close. A zero-PnL scratch counts as a loss."""
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    now = utc_now()
    rows = [
        ("a", -1.0, now - timedelta(minutes=5)),  # oldest
        ("b", -2.0, now - timedelta(minutes=4)),
        ("c",  3.0, now - timedelta(minutes=3)),  # break at winner
        ("d", -1.0, now - timedelta(minutes=2)),
        ("e",  0.0, now - timedelta(minutes=1)),  # zero counts as loss
        ("f", -1.0, now),                          # most recent
    ]
    with sqlite3.connect(settings.db_path) as conn:
        for mid, pnl, closed_at in rows:
            conn.execute(
                """
                insert into positions(
                    market_id, side, size_usd, entry_price, order_id, opened_at, status,
                    close_reason, closed_at, exit_price, realized_pnl
                ) values (?, ?, ?, ?, ?, ?, 'CLOSED', 'x', ?, 0.0, ?)
                """,
                (mid, "YES", 10.0, 0.50, f"o-{mid}", closed_at.isoformat(), closed_at.isoformat(), pnl),
            )
        conn.commit()
    # Scanning newest-first: f(loss), e(zero=loss), d(loss), c(win → break) → 3
    assert engine.get_consecutive_losses() == 3


def test_portfolio_consecutive_losses_zero_when_empty(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    assert engine.get_consecutive_losses() == 0


def test_portfolio_daily_realized_pnl_ignores_previous_days(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    opened_at = utc_now() - timedelta(days=1, minutes=5)
    closed_at = utc_now() - timedelta(days=1)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            """
            insert into positions(
                market_id, side, size_usd, entry_price, order_id, opened_at, status,
                close_reason, closed_at, exit_price, realized_pnl
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-1",
                SuggestedSide.YES.value,
                10.0,
                0.50,
                "paper-old",
                opened_at.isoformat(),
                "CLOSED",
                "ttl_expired",
                closed_at.isoformat(),
                0.40,
                -2.0,
            ),
        )
        conn.commit()
    account_state = engine.get_account_state(ExecutionMode.PAPER)
    assert account_state.daily_realized_pnl == 0.0
    assert engine.get_total_realized_pnl() == -2.0


def test_portfolio_counts_rejected_orders_without_counting_skips(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
    )
    failed_result = ExecutionResult(
        market_id="123",
        success=False,
        mode=ExecutionMode.LIVE,
        order_id="live-1",
        status="NOT_IMPLEMENTED",
        detail="disabled",
    )
    skipped_result = ExecutionResult(
        market_id="124",
        success=False,
        mode=ExecutionMode.PAPER,
        order_id="paper-2",
        status="SKIPPED",
        detail="not approved",
    )
    engine.record_execution(decision, failed_result)
    engine.record_execution(decision, skipped_result)
    account_state = engine.get_account_state(ExecutionMode.PAPER)
    assert account_state.rejected_orders == 1


def test_portfolio_tracks_live_order_submission(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
        asset_id="token-yes",
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-1",
        status="LIVE_SUBMITTED",
        detail="submitted",
    )
    engine.record_execution(decision, result)
    tracked = engine.list_live_orders()
    assert len(tracked) == 1
    assert tracked[0]["order_id"] == "live-1"
    assert tracked[0]["status"] == "LIVE_SUBMITTED"


def test_portfolio_updates_tracked_live_order(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
        asset_id="token-yes",
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-1",
        status="LIVE_SUBMITTED",
        detail="submitted",
    )
    engine.record_execution(decision, result)
    engine.update_live_order("live-1", status="MATCHED", detail="filled")
    tracked = engine.list_live_orders()
    assert tracked[0]["status"] == "MATCHED"
    assert tracked[0]["detail"] == "filled"


def test_portfolio_splits_active_and_terminal_live_orders(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
        asset_id="token-yes",
    )
    engine.record_execution(
        decision,
        ExecutionResult(
            market_id="123",
            success=True,
            mode=ExecutionMode.LIVE,
            order_id="live-active",
            status="LIVE_SUBMITTED",
            detail="submitted",
        ),
    )
    engine.record_execution(
        decision,
        ExecutionResult(
            market_id="123",
            success=True,
            mode=ExecutionMode.LIVE,
            order_id="live-terminal",
            status="MATCHED",
            detail="filled",
        ),
    )
    assert len(engine.list_active_live_orders()) == 1
    assert engine.list_active_live_orders()[0]["order_id"] == "live-active"
    assert len(engine.list_terminal_live_orders()) == 1
    assert engine.list_terminal_live_orders()[0]["order_id"] == "live-terminal"
