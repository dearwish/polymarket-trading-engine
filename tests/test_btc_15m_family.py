from __future__ import annotations

from pathlib import Path

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.connectors.polymarket import PolymarketConnector


def _settings(tmp_path: Path, family: str) -> Settings:
    return Settings(
        openrouter_api_key="",
        market_family=family,
        polymarket_private_key="",
        polymarket_funder="",
        polymarket_signature_type=0,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
    )


def test_btc_15m_scorer_matches_15_minute_phrase(tmp_path: Path) -> None:
    connector = PolymarketConnector(_settings(tmp_path, "btc_15m"))
    score = connector._btc_15m_match_score(
        "Bitcoin Up or Down - 10:30AM-10:45AM ET",
        "Resolves YES if BTC price is higher 15 minutes from market open.",
        "btc-updown-15m-1776522600",
    )
    assert score >= 3


def test_btc_15m_scorer_ignores_non_btc(tmp_path: Path) -> None:
    connector = PolymarketConnector(_settings(tmp_path, "btc_15m"))
    score = connector._btc_15m_match_score(
        "Will ETH be up 15 minutes from now?",
        "",
        "eth-15m",
    )
    assert score == 0


def test_btc_15m_scorer_rejects_non_15m_slug_decoys(tmp_path: Path) -> None:
    connector = PolymarketConnector(_settings(tmp_path, "btc_15m"))
    # Generic BTC "up or down" market without the canonical btc-updown-15m slug.
    # After Phase 7 we require the slug prefix — decoys must score 0.
    score = connector._btc_15m_match_score(
        "Bitcoin up or down in the next 10 minutes?",
        "",
        "btc-10m",
    )
    assert score == 0
    # A market with the right slug scores > 0.
    explicit = connector._btc_15m_match_score(
        "Bitcoin Up or Down - 11:00AM-11:15AM ET",
        "",
        "btc-updown-15m-1776524400",
    )
    assert explicit >= 3


def test_btc_15m_active_expiry_window(tmp_path: Path) -> None:
    connector = PolymarketConnector(_settings(tmp_path, "btc_15m"))
    assert connector._active_market_max_expiry_seconds() == 30 * 60


def test_btc_15m_discovery_request_limit_is_reasonable(tmp_path: Path) -> None:
    connector = PolymarketConnector(_settings(tmp_path, "btc_15m"))
    assert connector._discovery_request_limit(10) >= 200


def test_btc_15m_routes_through_family_score(tmp_path: Path) -> None:
    connector = PolymarketConnector(_settings(tmp_path, "btc_15m"))
    assert (
        connector._market_family_score("Bitcoin 15m up or down", "", "btc-15m")
        == connector._btc_15m_match_score("Bitcoin 15m up or down", "", "btc-15m")
    )
