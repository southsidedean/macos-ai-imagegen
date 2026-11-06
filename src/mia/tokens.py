"""Per-device tokens, and the browser sessions signed in with them. Each token is shown once and stored only as a hash,
and can be revoked on its own.

SHA-256 is enough here: a token is 256 random bits, so unlike a password it can't be guessed from a
list, and a slow hash would only slow every request.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mia import db
from mia.engine import MiaError

READ, GENERATE, ADMIN = "read", "generate", "admin"
SCOPES = (READ, GENERATE, ADMIN)  # each includes the ones before it
PREFIX = "mia_"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ISSUED = re.compile(PREFIX + r"[A-Za-z0-9_-]{43}")  # what create() makes: anything else needs no database to refuse
SESSION = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_DAYS = 3650
SESSION_DAYS = 90  # a token is shown only once, so a phone shouldn't need it again often
MAX_SESSIONS = 20  # per token: a client signing in forever mustn't fill the database
LAST_USED_EVERY = 60  # seconds: no need to write to the database on every request


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    scope: str
    created: str
    last_used: float | None
    revoked: str | None
    expires: float | None = None

    def allows(self, needed: str) -> bool:
        return SCOPES.index(self.scope) >= SCOPES.index(needed)


COLUMNS = "id, name, scope, created, last_used, revoked, expires"
ACTIVE = "revoked IS NULL AND (expires IS NULL OR expires > ?)"


def _digest(presented: str) -> str:
    return hashlib.sha256(presented.encode()).hexdigest()


def create(name: str, scope: str = GENERATE, days: int | None = None) -> str:
    """Makes a token and returns it. This is the only time anyone sees it. With `days`, it stops working after that."""
    if not NAME.fullmatch(name):
        raise MiaError("a device's name uses letters, digits, dots, dashes, and underscores, up to 64 characters")
    if scope not in SCOPES:
        raise MiaError(f"the scope must be one of: {', '.join(SCOPES)}")
    if days is not None and not 1 <= days <= MAX_DAYS:
        raise MiaError(f"a token lasts from 1 to {MAX_DAYS} days")
    expires = None if days is None else time.time() + days * 86400
    issued = PREFIX + secrets.token_urlsafe(32)
    try:
        with db.session() as connection:
            connection.execute(
                "INSERT INTO tokens (id, name, scope, digest, created, expires) VALUES (?, ?, ?, ?, ?, ?)",
                (secrets.token_hex(6), name, scope, _digest(issued), datetime.now(UTC).isoformat(timespec="seconds"), expires),
            )
    except sqlite3.IntegrityError as taken:
        raise MiaError(f"there's already a token for {name}: revoke it first, or choose another name") from taken
    return issued


def _found(connection: sqlite3.Connection, row: tuple[Any, ...] | None, now: float) -> Device | None:
    if row and (row[4] is None or now - row[4] > LAST_USED_EVERY):
        connection.execute("UPDATE tokens SET last_used = ? WHERE id = ?", (now, row[0]))
    return Device(*row) if row else None


def verify(presented: str) -> Device | None:
    """The device a token belongs to, or None. Looking up its hash means nothing compares the token itself."""
    if not ISSUED.fullmatch(presented):
        return None
    now = time.time()
    with db.session() as connection:
        row = connection.execute(
            f"SELECT {COLUMNS} FROM tokens WHERE digest = ? AND {ACTIVE}",  # nosec B608  # our own text
            (_digest(presented), now),
        ).fetchone()
        return _found(connection, row, now)


def start_session(device: Device) -> tuple[str, int]:
    """Makes a browser session for a signed-in device, and returns it and how many seconds it lasts. It can't outlast
    the token, and like a token it's stored only as a hash."""
    issued = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + SESSION_DAYS * 86400
    if device.expires is not None:
        expires = min(expires, device.expires)
    with db.session() as connection:
        connection.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
        connection.execute("INSERT INTO sessions (digest, token, expires) VALUES (?, ?, ?)", (_digest(issued), device.id, expires))
        connection.execute(
            "DELETE FROM sessions WHERE token = ? AND rowid NOT IN (SELECT rowid FROM sessions WHERE token = ? ORDER BY rowid DESC LIMIT ?)",
            (device.id, device.id, MAX_SESSIONS),
        )
    return issued, int(expires - now)


def verify_session(presented: str) -> Device | None:
    """The device a browser session belongs to, or None. A session ends with its token, however it was revoked."""
    if not SESSION.fullmatch(presented):
        return None
    now = time.time()
    with db.session() as connection:
        row = connection.execute(
            f"SELECT {COLUMNS} FROM tokens WHERE {ACTIVE} AND id = (SELECT token FROM sessions WHERE digest = ? AND expires > ?)",  # nosec B608
            (now, _digest(presented), now),
        ).fetchone()
        return _found(connection, row, now)


def end_session(presented: str) -> None:
    with db.session() as connection:
        connection.execute("DELETE FROM sessions WHERE digest = ?", (_digest(presented),))


def still_active(device_id: str, session: str | None = None) -> bool:
    """For anything long-lived that started signed in: a revoked token or a signed-out session has to stop working there too."""
    now = time.time()
    with db.session() as connection:
        row = connection.execute(f"SELECT 1 FROM tokens WHERE id = ? AND {ACTIVE}", (device_id, now)).fetchone()  # nosec B608
        if row is not None and session is not None:
            row = connection.execute("SELECT 1 FROM sessions WHERE digest = ? AND expires > ?", (_digest(session), now)).fetchone()
    return row is not None


def devices() -> list[Device]:
    with db.session() as connection:
        rows = connection.execute(f"SELECT {COLUMNS} FROM tokens ORDER BY created, rowid").fetchall()  # nosec B608
    return [Device(*row) for row in rows]


def revoke(name: str) -> None:
    with db.session() as connection:
        changed = connection.execute(
            "UPDATE tokens SET revoked = ? WHERE name = ? AND revoked IS NULL",
            (datetime.now(UTC).isoformat(timespec="seconds"), name),
        ).rowcount
        connection.execute("DELETE FROM sessions WHERE token IN (SELECT id FROM tokens WHERE name = ?)", (name,))
    if not changed:
        raise MiaError(f"there's no active token for {name}")
