from __future__ import annotations

import json
from pathlib import Path

from polymarket_trading_engine.engine.journal import Journal
from polymarket_trading_engine.engine.migrations import MigrationRunner


def _journal(tmp_path: Path, **kwargs) -> Journal:
    db_path = tmp_path / "agent.db"
    # Schema now lives in the migrations framework — run them before any
    # engine that touches the DB is constructed.
    MigrationRunner(db_path).run()
    return Journal(
        db_path=db_path,
        events_path=tmp_path / "events.jsonl",
        **kwargs,
    )


def test_read_recent_events_returns_last_n_without_loading_whole_file(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    for i in range(500):
        j.log_event("tick", {"i": i})
    tail = j.read_recent_events(limit=5)
    assert len(tail) == 5
    assert [event["payload"]["i"] for event in tail] == [495, 496, 497, 498, 499]


def test_read_recent_events_handles_huge_files_via_tail_read(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    # Write 2MB of synthetic JSONL lines without going through Journal so we
    # can simulate an already-large file left behind by a previous daemon run.
    with events_path.open("w") as handle:
        for i in range(20_000):
            handle.write(json.dumps({"event_type": "noise", "logged_at": "x", "payload": {"i": i}}) + "\n")
    MigrationRunner(tmp_path / "agent.db").run()
    j = Journal(db_path=tmp_path / "agent.db", events_path=events_path)
    tail = j.read_recent_events(limit=3)
    assert [event["payload"]["i"] for event in tail] == [19_997, 19_998, 19_999]


def test_read_recent_events_skips_malformed_lines(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    j.log_event("a", {"k": 1})
    j.events_path.open("a").write("not-json\n")
    j.log_event("b", {"k": 2})
    tail = j.read_recent_events(limit=10)
    # Malformed line is dropped; both real events survive.
    assert [event["event_type"] for event in tail] == ["a", "b"]


def test_prune_events_jsonl_noop_under_threshold(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    j.log_event("a", {"x": 1})
    initial_size = j.events_path.stat().st_size
    pruned = j.prune_events_jsonl(max_bytes=10 * 1024 * 1024)
    assert pruned is False
    assert j.events_path.stat().st_size == initial_size


def test_prune_events_jsonl_truncates_when_oversize(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    for i in range(5_000):
        j.log_event("tick", {"payload": "x" * 200, "i": i})
    size_before = j.events_path.stat().st_size
    assert size_before > 100_000
    pruned = j.prune_events_jsonl(max_bytes=50_000, keep_tail_bytes=20_000)
    assert pruned is True
    size_after = j.events_path.stat().st_size
    assert size_after < size_before
    assert size_after <= 20_000
    # Tail remains valid JSONL.
    tail = j.read_recent_events(limit=3)
    assert tail, "tail should still be parseable after prune"
    assert all("payload" in event for event in tail)


def test_log_event_auto_prunes_above_max_bytes(tmp_path: Path) -> None:
    j = _journal(
        tmp_path,
        events_jsonl_max_bytes=5_000,
        events_jsonl_keep_tail_bytes=2_000,
        prune_check_every=10,
    )
    for i in range(200):
        j.log_event("tick", {"payload": "y" * 200, "i": i})
    final_size = j.events_path.stat().st_size
    assert final_size < 20_000  # much smaller than the un-pruned run
    tail = j.read_recent_events(limit=5)
    assert tail  # still readable
