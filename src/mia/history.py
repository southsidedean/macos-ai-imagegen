"""Every image's prompt and settings, so any of them can be made again.

This is the one place prompts are kept: images are saved with no metadata. The queue joins this
database in 0.3.
"""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, dataclass, fields
from pathlib import Path

from mia.paths import make_private

SCHEMA = 1


@dataclass(frozen=True)
class Record:
    id: str
    created: str
    prompt: str
    seed: int
    width: int
    height: int
    steps: int
    model: str
    repo: str
    revision: str
    engine: str
    engine_version: str
    file: str
    pixels: str


def _connect(path: Path) -> sqlite3.Connection:
    make_private(path.parent)
    connection = sqlite3.connect(path)
    path.chmod(0o600)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA:
        connection.close()
        raise OSError(f"{path} is from a newer version of mia")
    if version == 0:
        columns = ", ".join(field.name + (" PRIMARY KEY" if field.name == "id" else "") for field in fields(Record))
        connection.execute(f"CREATE TABLE IF NOT EXISTS images ({columns})")  # nosec B608  # our own field names
        connection.execute(f"PRAGMA user_version = {SCHEMA}")
        connection.commit()
    return connection


def add(path: Path, record: Record) -> None:
    connection = _connect(path)
    with connection:
        marks = ", ".join("?" * len(fields(Record)))
        connection.execute(f"INSERT INTO images VALUES ({marks})", astuple(record))  # noqa: S608  # nosec B608  # only question marks
    connection.close()


def get(path: Path, image_id: str) -> Record | None:
    connection = _connect(path)
    row = connection.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    connection.close()
    return Record(*row) if row else None


def recent(path: Path, limit: int = 20) -> list[Record]:
    connection = _connect(path)
    rows = connection.execute("SELECT * FROM images ORDER BY created DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    connection.close()
    return [Record(*row) for row in rows]
