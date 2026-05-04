"""Tests for per-strategy paper bankrolls.

Locks in:

- ``parse_strategy_balance_overrides`` shape tolerance (whitespace,
  empty values, malformed pairs).
- ``resolve_starting_balance`` falls through to the default in every
  edge case (no settings ref, empty override string, missing strategy).
- ``get_account_state(strategy_id=...)`` honours the per-strategy
  override so MM and fade get truly independent paper bankrolls.
- Live-reload behaviour: changing ``settings.paper_starting_balance_per_strategy``
  is picked up on the next ``get_account_state`` call without
  re-constructing the PortfolioEngine.
"""
from __future__ import annotations

from polymarket_ai_agent.engine.portfolio import (
    PortfolioEngine,
    parse_strategy_balance_overrides,
)
from polymarket_ai_agent.types import (
    DecisionStatus,
    ExecutionMode,
    ExecutionResult,
    SuggestedSide,
    TradeDecision,
)


# ---------------------------------------------------------------------------
# parse_strategy_balance_overrides
# ---------------------------------------------------------------------------


def test_parse_empty_string_returns_empty_dict() -> None:
    assert parse_strategy_balance_overrides("") == {}


def test_parse_single_pair() -> None:
    assert parse_strategy_balance_overrides("market_maker:10000") == {
        "market_maker": 10000.0
    }


def test_parse_multiple_pairs_with_whitespace() -> None:
    out = parse_strategy_balance_overrides(" market_maker:10000 , fade:200 , penny:5 ")
    assert out == {"market_maker": 10000.0, "fade": 200.0, "penny": 5.0}


def test_parse_skips_malformed_pairs() -> None:
    """A typo on one entry must NOT blow up parsing of the others —
    operator-friendly: the dashboard accepts free-text input."""
    out = parse_strategy_balance_overrides(
        "market_maker:10000,broken,fade:200,no_value:,empty_key:50"
    )
    # Only well-formed pairs survive.
    assert out == {"market_maker": 10000.0, "fade": 200.0, "empty_key": 50.0}


def test_parse_skips_non_numeric_values() -> None:
    out = parse_strategy_balance_overrides("market_maker:not_a_number,fade:100")
    assert out == {"fade": 100.0}


# ---------------------------------------------------------------------------
# PortfolioEngine.resolve_starting_balance
# ---------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, raw: str = ""):
        self.paper_starting_balance_per_strategy = raw


def test_resolve_falls_back_to_default_when_no_settings_ref(settings) -> None:
    engine = PortfolioEngine(settings.db_path, 100.0)  # no settings= kwarg
    assert engine.resolve_starting_balance("market_maker") == 100.0
    assert engine.resolve_starting_balance(None) == 100.0


def test_resolve_falls_back_when_override_string_is_empty(settings) -> None:
    engine = PortfolioEngine(settings.db_path, 100.0, settings=_FakeSettings(""))
    assert engine.resolve_starting_balance("market_maker") == 100.0


def test_resolve_falls_back_when_strategy_not_in_map(settings) -> None:
    engine = PortfolioEngine(
        settings.db_path, 100.0, settings=_FakeSettings("market_maker:10000")
    )
    # MM gets the override.
    assert engine.resolve_starting_balance("market_maker") == 10000.0
    # fade — not in the map — falls back.
    assert engine.resolve_starting_balance("fade") == 100.0


def test_resolve_returns_default_for_none_strategy(settings) -> None:
    """``strategy_id=None`` (cross-strategy aggregate calls) always uses
    the default — there's no canonical "default" strategy to override.
    """
    engine = PortfolioEngine(
        settings.db_path, 100.0, settings=_FakeSettings("market_maker:10000")
    )
    assert engine.resolve_starting_balance(None) == 100.0


def test_resolve_picks_up_live_settings_change(settings) -> None:
    """Mutating the bound settings object's override string takes effect
    on the next call — same hot-reload pattern as other tunables.
    """
    fake = _FakeSettings("")
    engine = PortfolioEngine(settings.db_path, 100.0, settings=fake)
    assert engine.resolve_starting_balance("market_maker") == 100.0
    fake.paper_starting_balance_per_strategy = "market_maker:10000"
    assert engine.resolve_starting_balance("market_maker") == 10000.0


# ---------------------------------------------------------------------------
# get_account_state honours the per-strategy override
# ---------------------------------------------------------------------------


def test_account_state_uses_per_strategy_balance(settings) -> None:
    """An MM position reserves $1000; account_state should show MM
    available_usd = $10k − $1k = $9k while fade — sharing the same DB but
    with its own override — sees fade's own balance untouched.
    """
    engine = PortfolioEngine(
        settings.db_path,
        100.0,
        settings=_FakeSettings("market_maker:10000,fade:500"),
    )
    # Open a $1000 MM position.
    decision = TradeDecision(
        market_id="m1",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=1000.0,
        limit_price=0.52,
        rationale=[],
        rejected_by=[],
        strategy_id="market_maker",
    )
    result = ExecutionResult(
        market_id="m1",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-mm-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.52,
    )
    engine.record_execution(decision, result)

    mm_state = engine.get_account_state(ExecutionMode.PAPER, strategy_id="market_maker")
    fade_state = engine.get_account_state(ExecutionMode.PAPER, strategy_id="fade")
    aggregate_state = engine.get_account_state(ExecutionMode.PAPER)  # strategy_id=None

    # MM: $10k bankroll − $1000 reserved = $9000 available.
    assert mm_state.available_usd == 9000.0
    assert mm_state.open_positions == 1
    # fade: $500 bankroll − $0 reserved = $500. The MM position doesn't
    # contend against fade's pool — that's the whole point.
    assert fade_state.available_usd == 500.0
    assert fade_state.open_positions == 0
    # Aggregate (strategy_id=None) uses the default and counts ALL
    # reserved across strategies — informational/dashboard view.
    assert aggregate_state.available_usd == 100.0 - 1000.0  # default − total reserved
    assert aggregate_state.open_positions == 1


def test_account_state_falls_back_to_default_when_no_override(settings) -> None:
    """Old behaviour preserved: without an override map, every strategy
    sees the shared default bankroll. Existing soaks keep working as-is.
    """
    engine = PortfolioEngine(settings.db_path, 100.0, settings=_FakeSettings(""))
    decision = TradeDecision(
        market_id="m1",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=[],
        rejected_by=[],
        strategy_id="fade",
    )
    result = ExecutionResult(
        market_id="m1",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.52,
    )
    engine.record_execution(decision, result)
    state = engine.get_account_state(ExecutionMode.PAPER, strategy_id="fade")
    assert state.available_usd == 100.0 - 10.0
