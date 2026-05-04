from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from polymarket_trading_engine.apps.api.main import create_app
from polymarket_trading_engine.apps.daemon.heartbeat import HeartbeatWriter
from polymarket_trading_engine.apps.daemon.run import DaemonMetrics
from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.service import AgentService


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
        heartbeat_path=tmp_path / "data" / "heartbeat.json",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
    )
    base.update(overrides)
    return Settings(**base)


def _client(tmp_path: Path, **overrides) -> TestClient:
    settings = _settings(tmp_path, **overrides)
    service = AgentService(settings)
    app = create_app(
        service_factory=lambda: service,
        settings_factory=lambda: settings,
        base_settings_factory=lambda: settings,
    )
    return TestClient(app)


def test_api_metrics_json_exposes_gauges_and_row_counts(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "db_size_bytes" in body
    assert "events_jsonl_size_bytes" in body
    assert "row_counts" in body
    assert set(body["row_counts"].keys()) == {"positions", "order_attempts", "live_orders"}
    assert "exposure" in body
    assert "heartbeat_age_seconds" in body
    # No heartbeat has been written yet.
    assert body["heartbeat_age_seconds"] is None


def test_api_metrics_prometheus_format(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/metrics", params={"format": "prometheus"})
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "polymarket_agent_db_size_bytes" in body
    assert "polymarket_agent_events_jsonl_size_bytes" in body
    assert 'polymarket_agent_db_rows{table="positions"}' in body
    assert "polymarket_agent_safety_stop_triggered" in body


def test_api_metrics_picks_up_heartbeat_payload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    metrics = DaemonMetrics()
    metrics.polymarket_events = 42
    metrics.btc_ticks = 7
    metrics.decision_ticks = 3
    writer = HeartbeatWriter(settings.heartbeat_path)
    writer.write(metrics, extra={"market_family": "btc_5m"})
    app = create_app(
        service_factory=lambda: service,
        settings_factory=lambda: settings,
        base_settings_factory=lambda: settings,
    )
    client = TestClient(app)
    resp = client.get("/api/metrics")
    body = resp.json()
    assert body["heartbeat"]["metrics"]["polymarket_events"] == 42
    # Heartbeat age is small but set.
    assert body["heartbeat_age_seconds"] is not None
    assert body["heartbeat_age_seconds"] >= 0.0
    # Prometheus format surfaces the daemon-side counters.
    prom = client.get("/api/metrics", params={"format": "prometheus"}).text
    assert "polymarket_agent_polymarket_events" in prom
    assert "polymarket_agent_btc_ticks" in prom


def test_api_healthz_reports_ok_when_heartbeat_fresh(tmp_path: Path) -> None:
    settings = _settings(tmp_path, daemon_heartbeat_stale_seconds=30.0)
    service = AgentService(settings)
    HeartbeatWriter(settings.heartbeat_path).write(DaemonMetrics())
    app = create_app(
        service_factory=lambda: service,
        settings_factory=lambda: settings,
        base_settings_factory=lambda: settings,
    )
    client = TestClient(app)
    resp = client.get("/api/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["checks"]["heartbeat"]["ok"] is True


def test_api_healthz_flags_stale_heartbeat(tmp_path: Path) -> None:
    settings = _settings(tmp_path, daemon_heartbeat_stale_seconds=0.01)
    service = AgentService(settings)
    HeartbeatWriter(settings.heartbeat_path).write(DaemonMetrics())
    time.sleep(0.05)
    app = create_app(
        service_factory=lambda: service,
        settings_factory=lambda: settings,
        base_settings_factory=lambda: settings,
    )
    client = TestClient(app)
    resp = client.get("/api/healthz")
    body = resp.json()
    assert body["checks"]["heartbeat"]["ok"] is False
    assert body["ok"] is False


def test_api_healthz_flags_missing_heartbeat(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    app = create_app(
        service_factory=lambda: service,
        settings_factory=lambda: settings,
        base_settings_factory=lambda: settings,
    )
    client = TestClient(app)
    resp = client.get("/api/healthz")
    body = resp.json()
    assert body["checks"]["heartbeat"]["ok"] is False
    assert body["checks"]["heartbeat"]["age_seconds"] is None
