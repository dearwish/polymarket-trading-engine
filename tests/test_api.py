from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from polymarket_ai_agent.apps.api.main import create_app
from polymarket_ai_agent.config import Settings


class StubService:
    def status(self):
        return {"trading_mode": "paper", "open_positions": 0}

    def auth_status(self):
        return {"readonly_ready": True, "balance": 44.93}

    settings = Settings()

    def discover_markets(self):
        class Market:
            market_id = "123"
            question = "Will BTC be above 82k?"
            slug = "btc-above-82k"
            implied_probability = 0.42
            liquidity_usd = 1000.0
            volume_24h_usd = 5000.0
            end_date_iso = "2026-04-18T00:00:00Z"

        return [Market()]

    def get_active_market_id(self):
        return "active-123"

    def doctor(self, market_id=None):
        return {"readonly": True, "market_id": market_id or "active-123"}

    def live_activity(self, market_id=None, trade_limit=20, skip_scoring=False):
        return {
            "readonly": True,
            "market_id": market_id or "active-123",
            "last_poll": {
                "polled_at": "2026-04-17T00:00:00+00:00",
                "time_remaining_seconds": 1800,
                "time_remaining_minutes": 30.0,
                "trade_counts": {"yes": 2, "no": 1, "other": 0, "total": 3},
            },
            "preflight": {
                "blockers": [],
                "market": {
                    "question": "Will BTC be above 82k?",
                    "implied_probability": 0.42,
                    "liquidity_usd": 1000.0,
                    "seconds_to_expiry": 1800,
                },
                "assessment": {
                    "fair_probability": 0.55,
                    "confidence": 0.8,
                    "edge": 0.03,
                    "suggested_side": "YES",
                },
            },
            "recent_trades": {"count": 3},
            "tracked_orders": {"count": 0, "active_count": 0, "terminal_count": 0},
        }

    def live_reconcile(self, market_id=None, trade_limit=20, order_limit=50):
        return {
            "readonly": True,
            "market_id": market_id or "active-123",
            "tracked_orders": {"summary": {"active": 0, "terminal": 0, "errors": 0}},
            "preflight": {"blockers": []},
            "recent_trades": {"count": 0},
        }

    def live_preflight(self, market_id=None, skip_scoring=False):
        return {"readonly": True, "market_id": market_id or "active-123", "blockers": []}

    def live_orders(self):
        return {
            "readonly": True,
            "count": 1,
            "orders": [{"order_id": "live-1", "status": "OPEN"}],
        }

    def live_trades(self, market_id=None, limit=20):
        return {
            "readonly": True,
            "count": 1,
            "trades": [{"trade_id": "trade-1", "order_id": "live-1"}],
        }

    def generate_operator_report(self, session_id=None):
        return type(
            "Report",
            (),
            {
                "session_id": session_id or "session-1",
                "generated_at": type("Now", (), {"isoformat": lambda self: "2026-04-17T00:00:00+00:00"})(),
                "summary": "ok",
                "items": ["item-1"],
            },
        )()

    class Journal:
        @staticmethod
        def read_recent_events(limit=20):
            return [
                {"event_type": "simulation_cycle", "logged_at": "2026-04-17T00:00:00+00:00", "payload": {"market_id": "123"}},
                {"event_type": "market_assessment", "logged_at": "2026-04-17T00:01:00+00:00", "payload": {"market_id": "123"}},
                {"event_type": "daemon_tick", "logged_at": "2026-04-17T00:02:00+00:00", "payload": {"market_id": "123", "suggested_side": "YES", "edge_yes": 0.02}},
                {"event_type": "daemon_tick", "logged_at": "2026-04-17T00:03:00+00:00", "payload": {"market_id": "456", "suggested_side": "NO", "edge_no": 0.03}},
            ]

    journal = Journal()

    class Portfolio:
        def list_open_positions(self):
            from datetime import datetime, timezone
            from polymarket_ai_agent.types import SuggestedSide
            return [
                type(
                    "Position",
                    (),
                    {
                        "market_id": "open-1",
                        "side": SuggestedSide.YES,
                        "size_usd": 12.5,
                        "entry_price": 0.52,
                        "opened_at": datetime(2026, 4, 18, 22, 0, 0, tzinfo=timezone.utc),
                        "order_id": "paper-order-000001",
                        "strategy_id": "fade",
                    },
                )()
            ]

        def list_closed_positions(self, limit=100):
            return [
                type(
                    "Position",
                    (),
                    {
                        "market_id": "123",
                        "order_id": "paper-order-000001",
                        "side": type("Side", (), {"value": "YES"})(),
                        "size_usd": 10.0,
                        "entry_price": 0.4,
                        "exit_price": 0.55,
                        "opened_at": type("Now", (), {"isoformat": lambda self: "2026-04-17T00:00:00+00:00"})(),
                        "closed_at": type("Now", (), {"isoformat": lambda self: "2026-04-17T01:00:00+00:00"})(),
                        "close_reason": "manual_close",
                        "realized_pnl": 3.75,
                        "strategy_id": "fade",
                    },
                )()
            ]

        def get_total_realized_pnl(self):
            return 3.75

        def get_daily_realized_pnl(self):
            return 3.75

    portfolio = Portfolio()

    def simulate_market(self, market_id):
        class Candidate:
            question = "Will BTC be above 82k?"

        class Snapshot:
            candidate = Candidate()

        class Side:
            value = "YES"

        class Assessment:
            fair_probability = 0.55
            confidence = 0.8
            edge = 0.03
            suggested_side = Side()

        class Decision:
            status = type("Status", (), {"value": "APPROVED"})()
            side = Side()
            size_usd = 10.0
            limit_price = 0.42
            rejected_by = []

        return Snapshot(), Assessment(), Decision()


def test_api_health() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_api_status() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["trading_mode"] == "paper"


def test_api_markets() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/markets?limit=1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["markets"][0]["market_id"] == "123"


def test_api_doctor_active() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/doctor")
    assert response.status_code == 200
    assert response.json()["market_id"] == "active-123"


def test_api_live_activity_active() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/live/activity")
    assert response.status_code == 200
    assert response.json()["market_id"] == "active-123"
    assert response.json()["last_poll"]["trade_counts"]["yes"] == 2


def test_api_live_reconcile_active() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/live/reconcile")
    assert response.status_code == 200
    assert response.json()["tracked_orders"]["summary"]["active"] == 0


def test_api_report() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/report")
    assert response.status_code == 200
    assert response.json()["summary"] == "ok"


def test_api_recent_events() -> None:
    class ServiceWithEvents(StubService):
        class Journal:
            @staticmethod
            def read_recent_events(limit=20):
                return [{"event_type": "simulation_cycle", "logged_at": "2026-04-17T00:00:00+00:00", "payload": {"market_id": "123"}}]

        journal = Journal()

    client = TestClient(create_app(lambda: ServiceWithEvents()))
    response = client.get("/api/events/recent")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["events"][0]["event_type"] == "simulation_cycle"


def test_api_recent_decisions() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/decisions/recent?limit=2")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["decisions"][0]["event_type"] == "daemon_tick"


def test_api_live_orders() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/live/orders")
    assert response.status_code == 200
    assert response.json()["orders"][0]["order_id"] == "live-1"


def test_api_live_trades() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/live/trades")
    assert response.status_code == 200
    assert response.json()["trades"][0]["trade_id"] == "trade-1"


def test_api_simulate_active() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/simulate")
    assert response.status_code == 200
    payload = response.json()
    assert payload["market_id"] == "active-123"
    assert payload["decision"]["status"] == "APPROVED"


def test_api_portfolio_summary() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/portfolio/summary")
    assert response.status_code == 200
    payload = response.json()
    assert payload["open_positions"] == 1
    assert payload["total_realized_pnl"] == 3.75


def test_api_closed_positions() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/portfolio/closed-positions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["positions"][0]["cumulative_pnl"] == 3.75


def test_api_equity_curve() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/portfolio/equity-curve")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["points"][0]["equity"] == 3.75


def test_api_dashboard_snapshot() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["trading_mode"] == "paper"
    assert payload["auth"]["readonly_ready"] is True
    assert "settings" in payload
    assert payload["live_activity"]["market_id"] == "active-123"
    assert payload["recent_events"]["count"] == 4


def test_api_dashboard_handles_missing_active_market() -> None:
    class ServiceWithoutActiveMarket(StubService):
        def get_active_market_id(self):
            raise RuntimeError("No active market matched the configured market family.")

        def live_activity(self, market_id=None, trade_limit=20, skip_scoring=False):
            raise RuntimeError("No active market matched the configured market family.")

    client = TestClient(create_app(lambda: ServiceWithoutActiveMarket()))
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    payload = response.json()
    assert payload["live_activity"]["market_id"] == ""
    assert payload["live_activity"]["preflight"]["blockers"] == ["no_active_market"]


def test_api_live_activity_returns_404_when_active_market_missing() -> None:
    class ServiceWithoutActiveMarket(StubService):
        def get_active_market_id(self):
            raise RuntimeError("No active market matched the configured market family.")

    client = TestClient(create_app(lambda: ServiceWithoutActiveMarket()))
    response = client.get("/api/live/activity")
    assert response.status_code == 404
    assert "No active market matched" in response.json()["detail"]


def test_api_settings_round_trip(tmp_path: Path) -> None:
    from polymarket_ai_agent.engine.migrations import MigrationRunner
    from polymarket_ai_agent.engine.settings_store import SettingsStore
    from polymarket_ai_agent.config import load_runtime_overrides

    db_path = tmp_path / "data" / "agent.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    MigrationRunner(db_path).run()

    base_settings = Settings(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=db_path,
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
    )
    # settings_factory now materialises runtime overrides from the DB — the
    # same path `get_effective_settings()` uses in production.
    def effective_factory() -> Settings:
        overrides = load_runtime_overrides(base_settings)
        if not overrides:
            return base_settings
        return Settings.model_validate({**base_settings.model_dump(), **overrides})

    client = TestClient(
        create_app(
            lambda: StubService(),
            settings_factory=effective_factory,
            base_settings_factory=lambda: base_settings,
        )
    )
    response = client.get("/api/settings")
    assert response.status_code == 200
    # After migration, every editable field is overridden by the baseline
    # seed row — effective value = INITIAL_SETTINGS_BASELINE, not the
    # pydantic default on base_settings.
    from polymarket_ai_agent.initial_settings import INITIAL_SETTINGS_BASELINE
    assert response.json()["values"]["market_family"] == INITIAL_SETTINGS_BASELINE["market_family"]

    updated = client.put("/api/settings", json={"values": {"market_family": "btc_daily_threshold", "min_edge": 0.02}})
    assert updated.status_code == 200
    payload = updated.json()
    assert payload["values"]["market_family"] == "btc_daily_threshold"
    assert payload["overrides"]["min_edge"] == 0.02

    # Every PUT lands as an append-only row in settings_changes with source='api'.
    store = SettingsStore(db_path)
    recent = store.list_timeline()[-2:]
    assert {r.field for r in recent} == {"market_family", "min_edge"}
    assert all(r.source == "api" for r in recent)


def test_api_action_simulate_active() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.post("/api/actions/simulate-active", json={"active": True})
    assert response.status_code == 200
    assert response.json()["action"] == "simulate-active"
    assert response.json()["decision"]["status"] == "APPROVED"


def test_api_action_live_preflight() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.post("/api/actions/live-preflight", json={"active": True})
    assert response.status_code == 200
    assert response.json()["market_id"] == "active-123"


def test_api_action_live_reconcile() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.post("/api/actions/live-reconcile", json={"active": True})
    assert response.status_code == 200
    assert response.json()["tracked_orders"]["summary"]["terminal"] == 0


def test_api_action_live_watch() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.post("/api/actions/live-watch", json={"active": True, "iterations": 2, "interval_seconds": 0})
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "live-watch"
    assert payload["iterations_completed"] == 2
