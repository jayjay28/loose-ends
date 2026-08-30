"""Versioned migrations: fresh databases are stamped current without running
steps; existing databases are walked forward step by step."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from lifeline import db


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_db_stamped_current_without_running_steps(tmp_path, monkeypatch):
    # A step that would fail if executed against the fresh schema (duplicate
    # column) proves fresh DBs skip the steps entirely.
    monkeypatch.setattr(
        db, "MIGRATIONS", [["ALTER TABLE people ADD COLUMN display_name TEXT"]]
    )
    conn = db.connect(tmp_path / "fresh.db")
    db.migrate(conn)
    assert _version(conn) == 1
    conn.close()


def test_existing_db_walks_forward(tmp_path, monkeypatch):
    # Build a database at version 0 (no migrations defined yet).
    monkeypatch.setattr(db, "MIGRATIONS", [])
    path = tmp_path / "old.db"
    conn = db.connect(path)
    db.migrate(conn)
    assert _version(conn) == 0

    # Ship two migrations; reopening applies both, in order.
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [
            ["ALTER TABLE people ADD COLUMN test_a TEXT"],
            ["ALTER TABLE people ADD COLUMN test_b TEXT DEFAULT 'x'"],
        ],
    )
    db.migrate(conn)
    assert _version(conn) == 2
    cols = [r[1] for r in conn.execute("PRAGMA table_info(people)")]
    assert "test_a" in cols and "test_b" in cols

    # Re-running is a no-op (steps beyond user_version only).
    db.migrate(conn)
    assert _version(conn) == 2
    conn.close()


def test_partially_migrated_db_applies_only_remaining(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", [["ALTER TABLE people ADD COLUMN test_a TEXT"]])
    path = tmp_path / "mid.db"
    conn = db.connect(path)
    db.migrate(conn)  # fresh → stamped at 1, step skipped
    assert _version(conn) == 1

    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [
            ["ALTER TABLE people ADD COLUMN test_a TEXT"],  # must NOT re-run
            ["ALTER TABLE people ADD COLUMN test_b TEXT"],
        ],
    )
    db.migrate(conn)
    assert _version(conn) == 2
    cols = [r[1] for r in conn.execute("PRAGMA table_info(people)")]
    assert "test_a" not in cols  # skipped for the fresh DB, never re-run
    assert "test_b" in cols
    conn.close()


# --------------------------------------------------------------------------
# The real MIGRATIONS list, against a real old database.
#
# The three tests above prove the *machinery*: they swap in invented steps and
# check the walk is ordered, resumable and idempotent. None of them ever runs
# what actually ships. The conveyor belt was tested; the boxes on it were not.
#
# `fixtures/schema_v0.sql` is `lifeline/schema.sql` as it stood at b527be1,
# the commit before MIGRATIONS[0] was written — a database at version 0 is by
# definition one built from that file. Regenerate it with:
#
#     git show b527be1:backend/lifeline/schema.sql > tests/fixtures/schema_v0.sql
#
# Never edit it to match a later shape: its whole value is being the thing the
# migrations were written to upgrade *from*.
V0_SCHEMA = Path(__file__).resolve().parent / "fixtures" / "schema_v0.sql"


def _database_at_v0(path) -> sqlite3.Connection:
    conn = db.connect(path)
    conn.executescript(V0_SCHEMA.read_text())
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    return conn


def _shape(conn: sqlite3.Connection):
    """Every table, its columns with their declared types, and every index.

    Compared as sets rather than as the text of `sqlite_master`, because the
    same shape reached two ways is spelled differently — `CREATE TABLE` from
    schema.sql keeps its comments and formatting, `ALTER TABLE` does not.
    """
    tables = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        tables[name] = {
            row[1]: (row[2].upper(), row[3], row[4])   # name → (type, notnull, default)
            for row in conn.execute("PRAGMA table_info(%s)" % name)
        }
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
    }
    return tables, indexes


def test_real_migrations_walk_a_v0_database_forward(tmp_path):
    """Every shipped step, in order, against the schema it was written for.

    This is the one that stands between a bad step and the live database on the
    Mini — 21 MB of messages with no automated backup behind it. A migration
    that raises leaves that file stopped halfway: `migrate` commits after the
    loop, so the failing step's partial work is rolled back but every step
    before it is already applied and stamped.
    """
    conn = _database_at_v0(tmp_path / "old.db")
    db.migrate(conn)
    assert _version(conn) == len(db.MIGRATIONS)
    conn.close()


def test_upgraded_database_matches_a_fresh_one(tmp_path):
    """A database that walked here and one built here must be the same shape.

    Two ways to reach the current schema — apply every migration, or run
    schema.sql once — and nothing forces them to agree. When they disagree the
    two halves of the user base run on different databases, which is a bug that
    reproduces on one machine and not the other.

    It has already happened once: `idx_threads_contact` was created by the v10
    migration and never added to schema.sql, so every fresh install ran without
    it while every upgraded one had it.
    """
    old = _database_at_v0(tmp_path / "old.db")
    db.migrate(old)
    fresh = db.connect(tmp_path / "fresh.db")
    db.migrate(fresh)

    upgraded_tables, upgraded_indexes = _shape(old)
    fresh_tables, fresh_indexes = _shape(fresh)

    assert set(upgraded_tables) == set(fresh_tables)
    for table in sorted(upgraded_tables):
        assert upgraded_tables[table] == fresh_tables[table], "column drift in %s" % table
    assert upgraded_indexes == fresh_indexes

    old.close()
    fresh.close()
