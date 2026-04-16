from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, db_path: Path, events_path: Path):
        self.db_path = db_path
        self.events_path = events_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def log_event(self, event_type: str, payload: Any) -> None:
        event = {
            "event_type": event_type,
            "logged_at": self._utc_now_iso(),
            "payload": self._normalize(payload),
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def save_report(self, session_id: str, summary: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "insert into reports(session_id, summary, created_at) values (?, ?, ?)",
                (session_id, summary, self._utc_now_iso()),
            )
            conn.commit()

    def read_reports(self) -> list[tuple[str, str, str]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "select session_id, summary, created_at from reports order by rowid desc limit 20"
            ).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                create table if not exists reports (
                    session_id text not null,
                    summary text not null,
                    created_at text not null
                )
                """
            )
            conn.commit()

    def _normalize(self, payload: Any) -> Any:
        if is_dataclass(payload):
            return self._normalize(asdict(payload))
        if isinstance(payload, dict):
            return {key: self._normalize(value) for key, value in payload.items()}
        if isinstance(payload, list):
            return [self._normalize(item) for item in payload]
        if isinstance(payload, datetime):
            return payload.isoformat()
        return payload

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
