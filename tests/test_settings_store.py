from __future__ import annotations

from pathlib import Path

from polymarket_ai_agent.engine.migrations import MigrationRunner
from polymarket_ai_agent.engine.settings_store import SettingsStore


def _fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "agent.db"
    MigrationRunner(db).run()
    return db


def test_current_overrides_returns_latest_per_field(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    store = SettingsStore(db)
    # Baseline seed rows already exist — add a user-level override.
    store.record_changes([("min_edge", 0.10, 0.07)], source="api")
    assert store.current_overrides()["min_edge"] == 0.07
    # Another row: latest wins.
    store.record_changes([("min_edge", 0.07, 0.05)], source="cli")
    assert store.current_overrides()["min_edge"] == 0.05


def test_record_changes_is_atomic_per_call(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    store = SettingsStore(db)
    pre_id = store.get_max_id()
    ids = store.record_changes(
        [("min_edge", 0.10, 0.08), ("paper_trailing_stop_pct", 0.15, 0.10)],
        source="api",
        reason="eu session",
    )
    assert len(ids) == 2
    assert min(ids) == pre_id + 1
    # Verify both rows share the same changed_at (single transaction).
    rows = store.list_changes(since_id=pre_id)
    assert len(rows) == 2
    assert rows[0].changed_at == rows[1].changed_at
    assert all(r.source == "api" for r in rows)
    assert all(r.reason == "eu session" for r in rows)


def test_list_changes_since_id_is_monotonic(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    store = SettingsStore(db)
    first_ids = store.record_changes([("min_edge", 0.10, 0.05)], source="api")
    second_ids = store.record_changes([("min_edge", 0.05, 0.03)], source="cli")
    # Only the second batch shows up when since_id = first batch's id.
    after_first = store.list_changes(since_id=first_ids[-1])
    assert [r.id for r in after_first] == second_ids


def test_record_changes_empty_list_is_noop(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    store = SettingsStore(db)
    pre_id = store.get_max_id()
    assert store.record_changes([], source="api") == []
    assert store.get_max_id() == pre_id


def test_list_timeline_returns_all_rows_in_order(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    store = SettingsStore(db)
    store.record_changes([("min_edge", 0.10, 0.07)], source="api")
    store.record_changes([("min_edge", 0.07, 0.05)], source="api")
    timeline = store.list_timeline()
    # Baseline seed rows + two overrides; IDs strictly increasing.
    ids = [r.id for r in timeline]
    assert ids == sorted(ids)
    assert ids == list(range(1, len(ids) + 1))


def test_values_are_json_round_tripped(tmp_path: Path) -> None:
    """Booleans, floats, and strings all survive insert → select intact."""
    db = _fresh_db(tmp_path)
    store = SettingsStore(db)
    store.record_changes(
        [
            ("live_trading_enabled", False, True),
            ("min_edge", 0.10, 0.05),
            ("paper_tp_ladder", "0.30:0.5", "0.25:0.25,0.50:0.25"),
        ],
        source="api",
    )
    overrides = store.current_overrides()
    assert overrides["live_trading_enabled"] is True
    assert overrides["min_edge"] == 0.05
    assert overrides["paper_tp_ladder"] == "0.25:0.25,0.50:0.25"
