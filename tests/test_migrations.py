from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from polymarket_trading_engine.engine.migrations import (
    AppliedMigration,
    MigrationFailed,
    MigrationRunner,
)


def test_runs_all_initial_migrations_on_fresh_db(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    applied = MigrationRunner(db).run()

    assert [m.status for m in applied] == ["applied"] * len(applied)
    names = [m.name for m in applied]
    assert "20260421T130000-create-baseline-schema" in names
    assert "20260421T130500-create-settings-changes-table" in names
    assert "20260421T140000-seed-initial-settings-baseline" in names
    # Files are applied in chronological (filename) order.
    assert names == sorted(names)

    conn = sqlite3.connect(db)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"positions", "order_attempts", "live_orders", "reports", "settings_changes", "migrations"} <= tables
    n_baseline = conn.execute(
        "SELECT COUNT(*) FROM settings_changes WHERE source = 'baseline'"
    ).fetchone()[0]
    assert n_baseline > 0


def test_second_run_is_a_noop(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    runner = MigrationRunner(db)
    first = runner.run()
    second = runner.run()
    assert len(first) >= 3
    assert second == []


def test_existing_legacy_db_upgrades_cleanly(tmp_path: Path) -> None:
    """A DB that already has positions / reports tables (predating migrations)
    must accept the baseline migration as a no-op and still create the new
    settings_changes table.
    """
    db = tmp_path / "agent.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE positions (
                market_id text, side text, size_usd real, entry_price real,
                order_id text, opened_at text, status text,
                close_reason text, closed_at text, exit_price real,
                realized_pnl real
            )
            """
        )
        conn.execute("CREATE TABLE reports (session_id text, summary text, created_at text)")
        conn.commit()

    applied = MigrationRunner(db).run()
    # Every initial migration runs — baseline schema is a no-op on the
    # existing tables but still gets recorded; settings table + seeding run
    # for the first time.
    assert {m.name for m in applied} >= {
        "20260421T130000-create-baseline-schema",
        "20260421T130500-create-settings-changes-table",
        "20260421T140000-seed-initial-settings-baseline",
    }
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM settings_changes").fetchone()[0]
    assert n > 0


def test_broken_migration_raises_and_records_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A migration that raises must roll back, record the failure in the
    migrations table, and re-raise MigrationFailed so boot halts.
    """
    # Create a fake migrations package with a broken file. Reuse MigrationRunner
    # but point it at this custom package.
    pkg_dir = tmp_path / "fake_migrations"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "20260421T150000-setup.py").write_text(
        textwrap.dedent(
            """
            def upgrade(conn):
                conn.execute("CREATE TABLE ok (id INTEGER)")
            """
        )
    )
    (pkg_dir / "20260421T150500-broken.py").write_text(
        textwrap.dedent(
            """
            def upgrade(conn):
                raise RuntimeError("boom")
            """
        )
    )
    # Make the fake package importable.
    import sys

    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(
        "polymarket_trading_engine.engine.migrations._MIGRATION_NAME_RE",
        # allow the existing name shape
        __import__("re").compile(r"^\d{8}T\d{6}-[a-z0-9][a-z0-9\-]*$"),
    )
    try:
        db = tmp_path / "agent.db"
        runner = MigrationRunner(db, migrations_pkg="fake_migrations")
        with pytest.raises(MigrationFailed):
            runner.run()
        # The first migration succeeded; the second is recorded as 'failed'.
        conn = sqlite3.connect(db)
        rows = dict(conn.execute("SELECT name, status FROM migrations").fetchall())
        assert rows["20260421T150000-setup"] == "applied"
        assert rows["20260421T150500-broken"] == "failed"
    finally:
        sys.path.remove(str(tmp_path))


def test_failed_then_fixed_migration_reattempts(tmp_path: Path) -> None:
    """Operator fix: edit the broken migration, re-run, and the previously
    failed row is overwritten with the new success.
    """
    pkg_dir = tmp_path / "fix_migrations"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    broken_path = pkg_dir / "20260421T160000-fixable.py"
    broken_path.write_text("def upgrade(conn):\n    raise RuntimeError('first try')\n")

    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        db = tmp_path / "agent.db"
        runner = MigrationRunner(db, migrations_pkg="fix_migrations")
        with pytest.raises(MigrationFailed):
            runner.run()
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT status, error FROM migrations WHERE name = '20260421T160000-fixable'"
        ).fetchone()
        assert row[0] == "failed"
        assert "first try" in row[1]
        conn.close()

        # Fix the migration and re-run. Need a fresh module cache so the new
        # source is picked up.
        broken_path.write_text("def upgrade(conn):\n    conn.execute('CREATE TABLE ok (id INT)')\n")
        for modname in list(sys.modules):
            if modname.startswith("polymarket_trading_engine_migrations_") or modname == "fix_migrations":
                del sys.modules[modname]

        applied = MigrationRunner(db, migrations_pkg="fix_migrations").run()
        assert [m.name for m in applied] == ["20260421T160000-fixable"]
        assert applied[0].status == "applied"
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT status, error FROM migrations WHERE name = '20260421T160000-fixable'"
        ).fetchone()
        assert row == ("applied", None)
    finally:
        sys.path.remove(str(tmp_path))


def test_applied_migration_dataclass_fields() -> None:
    m = AppliedMigration(name="x", status="applied", duration_ms=5)
    assert m.name == "x"
    assert m.status == "applied"
    assert m.duration_ms == 5
    assert m.error is None
