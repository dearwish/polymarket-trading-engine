"""Tests for the MM universe scanner.

Two layers:

- ``score_mm_market`` — pure-math ranking, locked in for stability.
- ``PolymarketConnector.discover_mm_markets`` — exercises the Gamma
  ``/markets`` filter / rank pipeline against a stub HTTP transport so
  the test doesn't depend on live Polymarket or the family-slug filter.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.connectors.polymarket import PolymarketConnector
from polymarket_trading_engine.engine.market_maker.scanner import score_mm_market
from polymarket_trading_engine.types import MarketCandidate


def _candidate(
    market_id: str = "m",
    rewards_daily_rate: float = 10.0,
    liquidity_usd: float = 10_000.0,
) -> MarketCandidate:
    return MarketCandidate(
        market_id=market_id,
        question="",
        condition_id="c",
        slug="s",
        end_date_iso="2099-01-01T00:00:00Z",
        yes_token_id="y",
        no_token_id="n",
        implied_probability=0.5,
        liquidity_usd=liquidity_usd,
        volume_24h_usd=0.0,
        rewards_daily_rate=rewards_daily_rate,
        rewards_max_spread_pct=3.0,
        rewards_min_size=100.0,
    )


# ---------------------------------------------------------------------------
# score_mm_market
# ---------------------------------------------------------------------------


def test_score_zero_when_no_rewards() -> None:
    assert score_mm_market(_candidate(rewards_daily_rate=0.0)) == 0.0


def test_score_yields_more_for_thin_markets_at_same_rate() -> None:
    """Two markets paying $10/day each: the one with $5k liquidity beats
    the one with $50k liquidity (we'd be a bigger slice of the per-level
    reward pool on the thinner one).
    """
    thin = score_mm_market(_candidate(rewards_daily_rate=10.0, liquidity_usd=5_000.0))
    fat = score_mm_market(_candidate(rewards_daily_rate=10.0, liquidity_usd=50_000.0))
    assert thin > fat


def test_score_floors_liquidity_at_1k_to_avoid_inflation() -> None:
    """A $5/day reward on a $100 market shouldn't score 50× a $5/day on
    a $5k market just because the denominator is tiny — the floor caps
    that.
    """
    sub_floor = score_mm_market(_candidate(rewards_daily_rate=5.0, liquidity_usd=100.0))
    at_floor = score_mm_market(_candidate(rewards_daily_rate=5.0, liquidity_usd=1_000.0))
    assert sub_floor == at_floor


def test_score_scales_with_sqrt_of_rewards_at_fixed_liquidity() -> None:
    """Geometric-mean formula: doubling daily_rate gives sqrt(2)× score.
    Replaces the prior ``× linear`` formula which over-rewarded thin
    low-paying markets relative to fat high-paying ones (the saturation
    pattern the 2026-05-03 soak surfaced)."""
    import math

    s_low = score_mm_market(_candidate(rewards_daily_rate=5.0, liquidity_usd=20_000.0))
    s_high = score_mm_market(_candidate(rewards_daily_rate=50.0, liquidity_usd=20_000.0))
    expected_ratio = math.sqrt(50.0 / 5.0)
    assert abs(s_high / s_low - expected_ratio) < 1e-9


def test_score_compresses_rank_gap_between_thin_and_fat() -> None:
    """The geometric-mean formula (replacing the linear one as of
    2026-05-03) keeps thin markets ahead of fat-but-crowded markets —
    that's the correct economics, since our $1000 quote captures a
    bigger fraction of a thin pool. But the GAP is much smaller now,
    so the top-N pick gets a useful mix of yield tiers instead of
    concentrating in the thinnest markets only.

    Linear formula (old): thin/fat ratio = 20×
    sqrt formula (new) : thin/fat ratio ≈ 3-4×
    """
    import math

    fat = score_mm_market(_candidate(rewards_daily_rate=1250.0, liquidity_usd=700_000.0))
    thin = score_mm_market(_candidate(rewards_daily_rate=100.0, liquidity_usd=5_000.0))
    # Thin still wins (correct: more reward per dollar of competing book).
    assert thin > fat
    # But the gap is compressed — under the linear formula the ratio was
    # ~14×; with sqrt it should be under 5×.
    assert thin / fat < 5.0


# ---------------------------------------------------------------------------
# discover_mm_markets — uses a stub transport to fake the Gamma response.
# ---------------------------------------------------------------------------


def _settings(tmp_path) -> Settings:
    return Settings(
        openrouter_api_key="",
        market_family="btc_15m",
        polymarket_private_key="",
        polymarket_funder="",
        polymarket_signature_type=0,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
        heartbeat_path=tmp_path / "data" / "daemon_heartbeat.json",
    )


def _stub_market(
    *,
    market_id: str,
    slug: str,
    rewards_daily_rate: float,
    liquidity: float,
    tte_seconds: int,
    yes_token: str = "yes-tok",
    no_token: str = "no-tok",
    rewards_min_size: float = 100.0,
    rewards_max_spread: float = 3.0,
    rewards_shape: str = "clob_rewards",
) -> dict:
    """Build a fake Gamma response item.

    ``rewards_shape`` selects the JSON shape:
    - ``"clob_rewards"`` (default): the LIVE Gamma shape — top-level
      ``clobRewards[]`` + ``rewardsMaxSpread`` + ``rewardsMinSize``.
    - ``"legacy_nested"``: the older ``rewards.rates[]`` shape kept as
      a fallback path. Tests both to lock in parser tolerance.
    """
    end_iso = (
        (datetime.now(timezone.utc) + timedelta(seconds=tte_seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )
    base: dict = {
        "id": market_id,
        "conditionId": f"cond-{market_id}",
        "question": f"MM market {market_id}",
        "slug": slug,
        "endDate": end_iso,
        "clobTokenIds": json.dumps([yes_token, no_token]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "liquidityNum": liquidity,
        "volume24hr": 0.0,
    }
    if rewards_shape == "clob_rewards":
        base["clobRewards"] = [
            {
                "assetAddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                "rewardsDailyRate": rewards_daily_rate,
            }
        ]
        base["rewardsMaxSpread"] = rewards_max_spread
        base["rewardsMinSize"] = rewards_min_size
    elif rewards_shape == "legacy_nested":
        base["rewards"] = {
            "rates": [
                {
                    "asset_address": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                    "rewards_daily_rate": rewards_daily_rate,
                }
            ],
            "max_spread": rewards_max_spread,
            "min_size": rewards_min_size,
        }
    else:  # pragma: no cover
        raise ValueError(f"unknown shape {rewards_shape!r}")
    return base


def _stub_transport(payload: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _connector_with_stub(tmp_path, payload: list[dict]) -> PolymarketConnector:
    settings = _settings(tmp_path)
    client = httpx.Client(transport=_stub_transport(payload), timeout=2.0)
    return PolymarketConnector(settings, client=client)


def test_discover_filters_out_zero_rewards(tmp_path) -> None:
    payload = [
        _stub_market(
            market_id="paying",
            slug="pays-rewards",
            rewards_daily_rate=10.0,
            liquidity=20_000.0,
            tte_seconds=86_400,
        ),
        _stub_market(
            market_id="rewardless",
            slug="no-rewards",
            rewards_daily_rate=0.0,
            liquidity=20_000.0,
            tte_seconds=86_400,
        ),
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets(min_rewards_daily_usd=1.0)
    ids = [c.market_id for c in result]
    assert ids == ["paying"]


def test_discover_filters_out_thin_markets(tmp_path) -> None:
    payload = [
        _stub_market(
            market_id="thin",
            slug="thin",
            rewards_daily_rate=10.0,
            liquidity=500.0,
            tte_seconds=86_400,
        ),
        _stub_market(
            market_id="fat",
            slug="fat",
            rewards_daily_rate=10.0,
            liquidity=50_000.0,
            tte_seconds=86_400,
        ),
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets(min_liquidity_usd=5_000.0)
    assert [c.market_id for c in result] == ["fat"]


def test_discover_filters_out_short_tte(tmp_path) -> None:
    payload = [
        _stub_market(
            market_id="too-soon",
            slug="too-soon",
            rewards_daily_rate=10.0,
            liquidity=20_000.0,
            tte_seconds=600,  # 10 min — under the 1h floor
        ),
        _stub_market(
            market_id="far-future",
            slug="far-future",
            rewards_daily_rate=10.0,
            liquidity=20_000.0,
            tte_seconds=86_400,
        ),
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets(min_tte_seconds=3600)
    assert [c.market_id for c in result] == ["far-future"]


def test_discover_ranks_by_yield_per_liquidity_score(tmp_path) -> None:
    """Two reward-paying markets that meet all filters: the higher
    score_mm_market value comes first.
    """
    payload = [
        _stub_market(
            market_id="big-but-crowded",
            slug="b1",
            rewards_daily_rate=20.0,
            liquidity=200_000.0,  # score = 20*1000/200000 = 0.1
            tte_seconds=86_400,
        ),
        _stub_market(
            market_id="modest-reward-thinner",
            slug="b2",
            rewards_daily_rate=10.0,
            liquidity=10_000.0,  # score = 10*1000/10000 = 1.0
            tte_seconds=86_400,
        ),
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets()
    assert [c.market_id for c in result] == ["modest-reward-thinner", "big-but-crowded"]


def test_discover_caps_max_markets(tmp_path) -> None:
    payload = [
        _stub_market(
            market_id=f"m{i}",
            slug=f"s{i}",
            rewards_daily_rate=10.0,
            liquidity=10_000.0,
            tte_seconds=86_400,
        )
        for i in range(10)
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets(max_markets=3)
    assert len(result) == 3


def test_discover_returns_empty_when_api_unavailable(tmp_path) -> None:
    """Network failure must not crash the daemon — empty list signals
    "skip MM this cycle" cleanly.
    """
    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage")

    settings = _settings(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(fail_handler), timeout=2.0)
    connector = PolymarketConnector(settings, client=client)
    assert connector.discover_mm_markets() == []


def test_discover_filters_markets_above_size_floor(tmp_path) -> None:
    """When ``max_eligible_min_size_usd`` is supplied, drop markets whose
    ``rewardsMinSize`` exceeds it — those quotes earn the spread but no
    daily subsidy, defeating the strategy thesis.
    """
    payload = [
        _stub_market(
            market_id="cheap-ok",
            slug="cheap",
            rewards_daily_rate=10.0,
            liquidity=20_000.0,
            tte_seconds=86_400,
            rewards_min_size=50.0,
        ),
        _stub_market(
            market_id="expensive-skip",
            slug="expensive",
            rewards_daily_rate=10.0,
            liquidity=20_000.0,
            tte_seconds=86_400,
            rewards_min_size=1000.0,
        ),
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets(max_eligible_min_size_usd=100.0)
    assert [c.market_id for c in result] == ["cheap-ok"]


def test_discover_size_filter_disabled_when_none(tmp_path) -> None:
    """``max_eligible_min_size_usd=None`` opts out of the filter — useful
    for paper-mode soaks that just want to exercise the lifecycle.
    """
    payload = [
        _stub_market(
            market_id="expensive-but-allowed",
            slug="expensive",
            rewards_daily_rate=10.0,
            liquidity=20_000.0,
            tte_seconds=86_400,
            rewards_min_size=1000.0,
        ),
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets(max_eligible_min_size_usd=None)
    assert [c.market_id for c in result] == ["expensive-but-allowed"]


def test_parser_handles_live_clob_rewards_shape(tmp_path) -> None:
    """Lock in that the live ``clobRewards[]`` shape parses correctly.

    Regression guard: the original parser only knew the nested
    ``rewards.rates[]`` shape and silently returned ``daily_rate=0`` for
    every market in production until the 2026-05-01 fix.
    """
    payload = [
        _stub_market(
            market_id="live-shape",
            slug="live",
            rewards_daily_rate=1075.0,
            liquidity=712_327.0,
            tte_seconds=7 * 86_400,
            rewards_min_size=1000.0,
            rewards_max_spread=2.5,
            rewards_shape="clob_rewards",
        ),
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets()
    assert len(result) == 1
    cand = result[0]
    assert cand.rewards_daily_rate == 1075.0
    assert cand.rewards_max_spread_pct == 2.5
    assert cand.rewards_min_size == 1000.0


def test_parser_falls_back_to_legacy_nested_shape(tmp_path) -> None:
    """Older ``rewards.rates[]`` payloads must still parse — we keep the
    fallback so a Gamma rollback / regional shape variation doesn't
    silently zero out our scanner.
    """
    payload = [
        _stub_market(
            market_id="legacy-shape",
            slug="legacy",
            rewards_daily_rate=200.0,
            liquidity=20_000.0,
            tte_seconds=86_400,
            rewards_min_size=100.0,
            rewards_max_spread=3.5,
            rewards_shape="legacy_nested",
        ),
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets()
    assert len(result) == 1
    cand = result[0]
    assert cand.rewards_daily_rate == 200.0
    assert cand.rewards_max_spread_pct == 3.5
    assert cand.rewards_min_size == 100.0


def test_discover_bypasses_family_filter(tmp_path) -> None:
    """The MM scanner must NOT apply the BTC slug-pattern filter that
    the directional discovery path uses. A politics market with no BTC
    keywords must still surface here when it pays rewards.
    """
    payload = [
        _stub_market(
            market_id="politics",
            slug="will-something-political-happen-by-eoy",
            rewards_daily_rate=25.0,
            liquidity=80_000.0,
            tte_seconds=30 * 86_400,
        )
    ]
    connector = _connector_with_stub(tmp_path, payload)
    result = connector.discover_mm_markets()
    assert [c.market_id for c in result] == ["politics"]
