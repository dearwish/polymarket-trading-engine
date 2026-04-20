from __future__ import annotations

from pathlib import Path

import pytest

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.types import (
    MarketAssessment,
    MarketCandidate,
    MarketSnapshot,
    OrderBookSnapshot,
    SuggestedSide,
)


@pytest.fixture(autouse=True)
def _isolate_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test with cwd set to an empty temp dir so Settings() can't
    find the repo's .env file. Tests that need specific settings pass them
    explicitly; this keeps local operator tuning in .env from leaking in and
    silently breaking defaults-based test assertions.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    from polymarket_ai_agent.engine.migrations import MigrationRunner

    s = Settings(
        openrouter_api_key="",
        market_family="btc_5m",
        polymarket_private_key="",
        polymarket_funder="",
        polymarket_signature_type=0,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
    )
    # Schema is owned by the migrations framework; every engine that touches
    # the DB now assumes tables exist. Run migrations here so any test using
    # this fixture sees the full schema (incl. settings_changes seeded with
    # the baseline).
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    MigrationRunner(s.db_path).run()
    return s


@pytest.fixture()
def market_candidate() -> MarketCandidate:
    return MarketCandidate(
        market_id="123",
        question="Will BTC be above 100,000 in 5 minutes?",
        condition_id="cond-123",
        slug="btc-5m-test",
        end_date_iso="2099-01-01T00:00:00Z",
        yes_token_id="yes-token",
        no_token_id="no-token",
        implied_probability=0.52,
        liquidity_usd=25000.0,
        volume_24h_usd=50000.0,
        resolution_source="Resolves based on BTC price at expiry.",
    )


@pytest.fixture()
def market_snapshot(market_candidate: MarketCandidate) -> MarketSnapshot:
    return MarketSnapshot(
        candidate=market_candidate,
        orderbook=OrderBookSnapshot(
            bid=0.51,
            ask=0.53,
            midpoint=0.52,
            spread=0.02,
            depth_usd=400.0,
            last_trade_price=0.52,
        ),
        seconds_to_expiry=120,
        recent_price_change_bps=12.5,
        recent_trade_count=7,
        external_price=101000.0,
    )


@pytest.fixture()
def market_assessment(market_candidate: MarketCandidate) -> MarketAssessment:
    return MarketAssessment(
        market_id=market_candidate.market_id,
        fair_probability=0.58,
        confidence=0.82,
        suggested_side=SuggestedSide.YES,
        expiry_risk="LOW",
        reasons_for_trade=["Positive short-horizon signal."],
        reasons_to_abstain=[],
        edge=0.06,
        raw_model_output="{}",
    )
