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


def test_api_simulate_active() -> None:
    client = TestClient(create_app(lambda: StubService()))
    response = client.get("/api/simulate")
    assert response.status_code == 200
    payload = response.json()
    assert payload["market_id"] == "active-123"
    assert payload["decision"]["status"] == "APPROVED"
