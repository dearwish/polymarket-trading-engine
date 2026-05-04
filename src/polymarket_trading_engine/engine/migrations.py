"""Python-only schema migrations runner.

Migrations live in ``src/polymarket_trading_engine/migrations/`` as
``YYYYMMDDTHHMMSS-<dashed-description>.py`` files. Each exports
``upgrade(conn: sqlite3.Connection) -> None``; the runner opens the
transaction, calls the function, commits on success, and records a row in
``migrations``. A failed migration rolls back, records the traceback in
``migrations.status='failed'``, and raises — halting boot so the operator
sees the problem before engines come up.

Runner invariants:

- One transaction per migration — either the whole file succeeds or none of
  its effects persist.
- Applied migrations are immutable: editing an already-applied file is the
  operator's problem. A future enhancement can detect checksum mismatches.
- Files are discovered via ``importlib.resources`` so the framework works
  under both editable installs and zipped wheels.
"""
from __future__ import annotations

import importlib.resources
import importlib.util
import re
import sqlite3
import sys
import time
import traceback
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from polymarket_trading_engine.engine.db import configure_connection


_MIGRATION_NAME_RE = re.compile(r"^\d{8}T\d{6}-[a-z0-9][a-z0-9\-]*$")


@dataclass(slots=True)
class AppliedMigration:
    name: str
    status: str
    duration_ms: int
    error: str | None = None


class MigrationFailed(RuntimeError):
    """Raised when a migration's ``upgrade()`` call raises.

    The runner records the failure in the ``migrations`` table before
    re-raising, so the operator has a persistent record of what blew up.
    """


class MigrationRunner:
    def __init__(
        self,
        db_path: Path,
        migrations_pkg: str = "polymarket_trading_engine.migrations",
    ):
        self.db_path = Path(db_path)
        self.migrations_pkg = migrations_pkg

    # --- public ---------------------------------------------------------

    def run(self) -> list[AppliedMigration]:
        """Apply every migration not yet in ``migrations.status='applied'``.

        Returns the list of migrations applied in this call (possibly empty
        when the DB is up to date). Raises :class:`MigrationFailed` if any
        step fails.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        applied_this_run: list[AppliedMigration] = []
        migrations = self._discover_migrations()
        self._stem_to_path = {stem: path for stem, path in migrations}
        with closing(sqlite3.connect(self.db_path)) as conn:
            configure_connection(conn)
            self._ensure_migrations_table(conn)
            already = self._already_applied(conn)
            for name in (stem for stem, _ in migrations):
                if name in already:
                    continue
                result = self._apply_one(conn, name)
                applied_this_run.append(result)
                if result.status == "failed":
                    raise MigrationFailed(
                        f"Migration {name} failed: {result.error}. "
                        "Fix the file and restart the service."
                    )
        return applied_this_run

    # --- internals ------------------------------------------------------

    @staticmethod
    def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
        # Self-bootstrap: the table recording applied migrations is itself
        # created outside the migrations framework. No chicken-and-egg.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS migrations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                applied_at  TEXT NOT NULL,
                status      TEXT NOT NULL,
                error       TEXT,
                duration_ms INTEGER
            )
            """
        )
        conn.commit()

    @staticmethod
    def _already_applied(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM migrations WHERE status = 'applied'"
        ).fetchall()
        return {str(r[0]) for r in rows}

    def _discover_migrations(self) -> list[tuple[str, Path]]:
        """Return (stem, filesystem path) pairs sorted chronologically.

        Only files whose *stem* matches ``<timestamp>-<slug>`` are picked up;
        ``__init__.py`` and any scratch files are ignored. Returns paths so
        the loader can use ``spec_from_file_location`` — module names with
        hyphens aren't valid Python identifiers, so we can't import via
        ``importlib.import_module`` with the dotted-name form.
        """
        pkg_files = importlib.resources.files(self.migrations_pkg)
        results: list[tuple[str, Path]] = []
        for entry in pkg_files.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.endswith(".py"):
                continue
            stem = entry.name[:-3]
            if not _MIGRATION_NAME_RE.match(stem):
                continue
            # Materialise a filesystem path; works for editable installs and
            # unpacked wheels. Zipped distributions would need a different
            # loader — out of scope since we ship as an editable package.
            with importlib.resources.as_file(entry) as fs_path:
                results.append((stem, Path(fs_path)))
        return sorted(results, key=lambda pair: pair[0])

    @staticmethod
    def _load_migration_module(stem: str, path: Path) -> ModuleType:
        """Load a migration module from disk, bypassing the dotted-name
        import system so hyphens in the filename are allowed.
        """
        safe_name = "polymarket_trading_engine_migrations_" + stem.replace("-", "_")
        spec = importlib.util.spec_from_file_location(safe_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot build loader spec for migration {path}")
        module = importlib.util.module_from_spec(spec)
        # Register so intra-migration imports (rare) resolve.
        sys.modules[safe_name] = module
        spec.loader.exec_module(module)
        return module

    def _apply_one(self, conn: sqlite3.Connection, name: str) -> AppliedMigration:
        started = time.perf_counter()
        try:
            path = self._stem_to_path[name]
            module = self._load_migration_module(name, path)
            upgrade = getattr(module, "upgrade", None)
            if not callable(upgrade):
                raise AttributeError(
                    f"Migration {name} has no callable upgrade(conn) function"
                )
            # Explicit transaction so a mid-migration failure rolls back the
            # whole file's DDL + data effects. Commit only on clean return.
            conn.execute("BEGIN")
            upgrade(conn)
            duration_ms = int((time.perf_counter() - started) * 1000)
            # UPSERT so a previously-failed row is overwritten by the
            # success row on re-run after a fix.
            conn.execute(
                """
                INSERT INTO migrations(name, applied_at, status, error, duration_ms)
                VALUES (?, ?, 'applied', NULL, ?)
                ON CONFLICT(name) DO UPDATE SET
                    applied_at = excluded.applied_at,
                    status = excluded.status,
                    error = excluded.error,
                    duration_ms = excluded.duration_ms
                """,
                (name, _utc_now_iso(), duration_ms),
            )
            conn.commit()
            return AppliedMigration(name=name, status="applied", duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001 — we record and re-raise below
            duration_ms = int((time.perf_counter() - started) * 1000)
            tb = traceback.format_exc()
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            # Record the failure in its own transaction so the row survives.
            # UPSERT so re-runs overwrite the previous failure message.
            conn.execute(
                """
                INSERT INTO migrations(name, applied_at, status, error, duration_ms)
                VALUES (?, ?, 'failed', ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    applied_at = excluded.applied_at,
                    status = excluded.status,
                    error = excluded.error,
                    duration_ms = excluded.duration_ms
                """,
                (name, _utc_now_iso(), tb, duration_ms),
            )
            conn.commit()
            return AppliedMigration(
                name=name, status="failed", duration_ms=duration_ms, error=str(exc)
            )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
