from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(payload: Any) -> Any:
    if is_dataclass(payload):
        return _normalize(asdict(payload))
    if isinstance(payload, dict):
        return {key: _normalize(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_normalize(item) for item in payload]
    if isinstance(payload, datetime):
        return payload.isoformat()
    return payload


class HeartbeatWriter:
    """Atomically persists the daemon's current status to a JSON file.

    The API is a separate process from the daemon, so in-memory
    :class:`DaemonMetrics` cannot be read directly from ``/api/metrics``.
    The daemon calls :py:meth:`write` at a steady cadence; the API reads the
    file on each request to compute freshness. Writes use a tmp-file +
    rename so partial reads are impossible.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, metrics: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "written_at": _utc_now_iso(),
            "metrics": _normalize(metrics),
        }
        if extra:
            payload.update(_normalize(extra))
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self.path.parent),
            delete=False,
            encoding="utf-8",
            suffix=".tmp",
        )
        try:
            json.dump(payload, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self.path)
        except Exception:
            try:
                Path(tmp.name).unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass
            raise
        return payload


class HeartbeatReader:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def age_seconds(self, now: datetime | None = None) -> float | None:
        payload = self.read()
        if not payload:
            return None
        written_at = payload.get("written_at")
        if not written_at:
            return None
        try:
            ts = datetime.fromisoformat(str(written_at))
        except ValueError:
            return None
        current = now or datetime.now(timezone.utc)
        return max(0.0, (current - ts).total_seconds())
