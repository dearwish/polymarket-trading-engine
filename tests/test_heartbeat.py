from __future__ import annotations

import json
import time
from pathlib import Path

from polymarket_ai_agent.apps.daemon.heartbeat import HeartbeatReader, HeartbeatWriter
from polymarket_ai_agent.apps.daemon.run import DaemonMetrics


def test_heartbeat_write_produces_atomic_file(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    writer = HeartbeatWriter(path)
    metrics = DaemonMetrics()
    metrics.polymarket_events = 7
    payload = writer.write(metrics, extra={"market_family": "btc_5m"})
    assert path.exists()
    assert payload["metrics"]["polymarket_events"] == 7
    assert payload["market_family"] == "btc_5m"
    on_disk = json.loads(path.read_text())
    assert on_disk["metrics"]["polymarket_events"] == 7


def test_heartbeat_reader_returns_none_when_file_missing(tmp_path: Path) -> None:
    reader = HeartbeatReader(tmp_path / "nope.json")
    assert reader.read() is None
    assert reader.age_seconds() is None


def test_heartbeat_reader_returns_age_in_seconds(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    writer = HeartbeatWriter(path)
    writer.write(DaemonMetrics())
    reader = HeartbeatReader(path)
    time.sleep(0.05)
    age = reader.age_seconds()
    assert age is not None
    assert 0.0 < age < 5.0


def test_heartbeat_reader_tolerates_corrupted_file(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("{not-json")
    reader = HeartbeatReader(path)
    assert reader.read() is None
    assert reader.age_seconds() is None
