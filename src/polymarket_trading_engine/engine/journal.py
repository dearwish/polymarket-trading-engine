from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_trading_engine.engine.db import configure_connection


class Journal:
    def __init__(
        self,
        db_path: Path,
        events_path: Path,
        events_jsonl_max_bytes: int = 0,
        events_jsonl_keep_tail_bytes: int = 0,
        prune_check_every: int = 200,
    ):
        self.db_path = db_path
        self.events_path = events_path
        self.events_jsonl_max_bytes = events_jsonl_max_bytes
        self.events_jsonl_keep_tail_bytes = events_jsonl_keep_tail_bytes
        self._prune_check_every = max(1, prune_check_every)
        self._writes_since_prune = 0
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
        self._maybe_prune()

    def _maybe_prune(self) -> None:
        if self.events_jsonl_max_bytes <= 0:
            return
        self._writes_since_prune += 1
        if self._writes_since_prune < self._prune_check_every:
            return
        self._writes_since_prune = 0
        self.prune_events_jsonl(
            self.events_jsonl_max_bytes,
            keep_tail_bytes=self.events_jsonl_keep_tail_bytes or None,
        )

    def events_jsonl_size_bytes(self) -> int:
        if not self.events_path.exists():
            return 0
        return self.events_path.stat().st_size

    def db_size_bytes(self) -> int:
        if not self.db_path.exists():
            return 0
        total = self.db_path.stat().st_size
        for suffix in ("-wal", "-shm"):
            sidecar = self.db_path.with_suffix(self.db_path.suffix + suffix)
            if sidecar.exists():
                total += sidecar.stat().st_size
        return total

    def vacuum(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("vacuum")
            conn.execute("pragma wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def save_report(self, session_id: str, summary: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "insert into reports(session_id, summary, created_at) values (?, ?, ?)",
                (session_id, summary, self._utc_now_iso()),
            )
            conn.commit()

    def read_recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the last ``limit`` events without loading the whole file.

        The naive ``read_text().splitlines()[-limit:]`` approach held the
        entire events.jsonl in memory, which is fatal once a long-running
        daemon writes multi-GB of ticks. Reading from the tail in fixed-size
        chunks keeps the cost bounded regardless of file size.
        """
        if not self.events_path.exists() or limit <= 0:
            return []
        lines = self._tail_lines(self.events_path, limit)
        events: list[dict[str, Any]] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
        return events

    def prune_events_jsonl(self, max_bytes: int, keep_tail_bytes: int | None = None) -> bool:
        """Truncate events.jsonl to the last ``keep_tail_bytes`` when oversize.

        Returns ``True`` if the file was pruned, ``False`` otherwise. A single
        long-running daemon can produce tens of GB of JSONL in a week; this is
        the minimal retention knob that keeps disk usage bounded without a
        separate log-rotation process.
        """
        if not self.events_path.exists() or max_bytes <= 0:
            return False
        size = self.events_path.stat().st_size
        if size <= max_bytes:
            return False
        keep = keep_tail_bytes if keep_tail_bytes is not None else max_bytes // 2
        keep = max(0, min(keep, size))
        with self.events_path.open("rb") as src:
            src.seek(-keep, 2) if keep > 0 else src.seek(0)
            data = src.read()
        # Drop any partial first line so we only keep complete JSONL records.
        newline_index = data.find(b"\n") if keep > 0 else -1
        if newline_index != -1:
            data = data[newline_index + 1 :]
        tmp_path = self.events_path.with_suffix(self.events_path.suffix + ".prune-tmp")
        tmp_path.write_bytes(data)
        tmp_path.replace(self.events_path)
        return True

    @staticmethod
    def _tail_lines(path: Path, limit: int, chunk_size: int = 64 * 1024) -> list[str]:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            remaining = handle.tell()
            buffer = b""
            newline_count = 0
            while remaining > 0 and newline_count <= limit:
                read_size = min(chunk_size, remaining)
                remaining -= read_size
                handle.seek(remaining)
                chunk = handle.read(read_size)
                buffer = chunk + buffer
                newline_count = buffer.count(b"\n")
        text = buffer.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-limit:]

    def read_reports(self) -> list[tuple[str, str, str]]:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            rows = conn.execute(
                "select session_id, summary, created_at from reports order by rowid desc limit 20"
            ).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]

    def _init_db(self) -> None:
        # Schema owned by migrations; here we only apply pragmas and verify.
        with closing(sqlite3.connect(self.db_path)) as conn:
            configure_connection(conn)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reports'"
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "Journal: `reports` table missing. "
                    "Run MigrationRunner before constructing the Journal."
                )

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
