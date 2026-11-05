"""Every image's prompt and settings, so any of them can be made again.

The pictures themselves hold no metadata. Prompts are kept here and on jobs, in the same database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import astuple, dataclass, fields
from pathlib import Path

from mia import db


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


def add(path: Path, record: Record, then: Callable[[sqlite3.Connection, Record], None] | None = None) -> None:
    """Records an image. `then` does more in the same transaction, so both happen or neither does."""
    with db.session(path) as connection:
        marks = ", ".join("?" * len(fields(Record)))
        connection.execute(f"INSERT INTO images VALUES ({marks})", astuple(record))  # nosec B608  # only question marks
        if then is not None:
            then(connection, record)


def get(path: Path, image_id: str) -> Record | None:
    with db.session(path) as connection:
        row = connection.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    return Record(*row) if row else None


def recent(path: Path, limit: int = 20, offset: int = 0, search: str = "") -> list[Record]:
    """Newest first, optionally only those whose prompt holds some text, taken literally."""
    escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with db.session(path) as connection:
        rows = connection.execute(
            "SELECT * FROM images WHERE prompt LIKE ? ESCAPE '\\' ORDER BY created DESC, rowid DESC LIMIT ? OFFSET ?",
            (f"%{escaped}%", limit, offset),
        ).fetchall()
    return [Record(*row) for row in rows]


def delete(path: Path, image_id: str) -> None:
    """Forgets an image, and the job that made it, which holds the same prompt."""
    with db.session(path) as connection:
        connection.execute("DELETE FROM images WHERE id = ?", (image_id,))
        connection.execute("DELETE FROM jobs WHERE image = ?", (image_id,))
