"""mia's one database: images, jobs, and tokens. Two processes use it, so it's in WAL mode."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mia import paths

# Each entry takes the database one version on. Add to the end, and never change an old one.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE IF NOT EXISTS images (id PRIMARY KEY, created, prompt, seed, width, height, steps, model,
        repo, revision, engine, engine_version, file, pixels)""",
    ),
    (
        """CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT NOT NULL, created TEXT NOT NULL, started TEXT,
        finished TEXT, prompt TEXT NOT NULL, seed INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
        steps INTEGER NOT NULL, model TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0, image TEXT, error TEXT,
        cancel_requested REAL, token TEXT, idempotency_key TEXT, planned_image TEXT)""",
        "CREATE UNIQUE INDEX jobs_idempotency ON jobs (token, idempotency_key) WHERE idempotency_key IS NOT NULL",
        "CREATE INDEX jobs_state ON jobs (state, created)",
        """CREATE TABLE tokens (id TEXT PRIMARY KEY, name TEXT NOT NULL, scope TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
        created TEXT NOT NULL, last_used REAL, revoked TEXT)""",
        "CREATE UNIQUE INDEX tokens_active_name ON tokens (name) WHERE revoked IS NULL",
    ),
    ("ALTER TABLE tokens ADD COLUMN expires REAL",),
)
SCHEMA = len(MIGRATIONS)


def _migrate(connection: sqlite3.Connection, path: Path) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA:
        return
    if version > SCHEMA:
        raise OSError(f"{path} is from a newer version of mia")
    # BEGIN IMMEDIATE, and read the version again inside it: the server and worker may start together.
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        for statements in MIGRATIONS[version:]:
            for statement in statements:
                connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _use_wal(connection: sqlite3.Connection) -> None:
    # Moving a database to WAL needs it alone for a moment, and SQLite's busy timeout doesn't cover that, so two
    # processes opening a new database together can refuse each other. It only happens once per database.
    for attempt in range(50):
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as problem:
            if attempt == 49 or "locked" not in str(problem):  # anything else won't clear by waiting
                raise
            time.sleep(0.1)


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or paths.history_file()
    paths.make_private(path.parent)
    connection = sqlite3.connect(path, timeout=10)
    try:
        path.chmod(0o600)
        _use_wal(connection)
        _migrate(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A connection whose work is committed on success and rolled back on an error."""
    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
