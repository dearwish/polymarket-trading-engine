from __future__ import annotations

from fastapi.testclient import TestClient

from polymarket_ai_agent.apps.api.main import create_app


class StubService:
    def status(self):
        return {"trading_mode": "paper", "open_positions": 0}

    def auth_status(self):
        return {"readonly_ready": True, "balance": 44.93}

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

    def live_activity(self, market_id=None, trade_limit=20):
        return {"readonly": True, "market_id": market_id or "active-123", "recent_trades": {"count": 0}}

    def live_reconcile(self, market_id=None, trade_limit=20, order_limit=50):
        return {
            "readonly": True,
            "market_id": market_id or "active-123",
            "tracked_orders": {"summary": {"active": 0, "terminal": 0, "errors": 0}},
        }

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
            ]

    journal = Journal()

    class Portfolio:
        def list_open_positions(self):
            return [type("Position", (), {"size_usd": 12.5})()]

        def list_closed_positions(self, limit=100):
            return [
                type(
                    "Position",
                    (),
                    {
                        "market_id": "123",
                        "side": type("Side", (), {"value": "YES"})(),
                        "size_usd": 10.0,
                        "entry_price": 0.4,
                        "exit_price": 0.55,
                        "opened_at": type("Now", (), {"isoformat": lambda self: "2026-04-17T00:00:00+00:00"})(),
                        "closed_at": type("Now", (), {"isoformat": lambda self: "2026-04-17T01:00:00+00:00"})(),
                        "close_reason": "manual_close",
                        "realized_pnl": 3.75,
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
    assert payload["decisions"][0]["event_type"] == "simulation_cycle"


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
