from __future__ import annotations

import json

from polymarket_trading_engine.engine.journal import Journal


def test_journal_logs_event_and_reads_reports(settings) -> None:
    journal = Journal(settings.db_path, settings.events_path)
    journal.log_event("test_event", {"hello": "world"})
    journal.save_report("session-1", "first summary")

    content = settings.events_path.read_text(encoding="utf-8").strip().splitlines()
    parsed = json.loads(content[0])
    assert parsed["event_type"] == "test_event"
    assert parsed["payload"]["hello"] == "world"

    events = journal.read_recent_events(limit=5)
    assert events[0]["event_type"] == "test_event"

    reports = journal.read_reports()
    assert reports[0][0] == "session-1"
    assert reports[0][1] == "first summary"
