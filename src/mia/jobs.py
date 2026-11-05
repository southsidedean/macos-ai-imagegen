"""The queue. A job is queued, then running, then done, failed, or cancelled, and it never goes back.

A job that was running when the worker died is failed with the reason, not retried: whatever killed
the worker, most often memory, would likely kill it again.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mia import db
from mia.engine import MiaError, Request

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
FINISHED = (DONE, FAILED, CANCELLED)
MAX_QUEUED = 20
KEEP_FINISHED_DAYS = 30  # jobs hold prompts, and nothing else needs them for long: images keep their own record
COLUMNS = (
    "id, state, created, started, finished, prompt, seed, width, height, steps, model, step, image, error, "
    "cancel_requested, token, idempotency_key, planned_image"
)


class QueueFull(MiaError):
    pass


class Conflict(MiaError):
    """The request can't apply to the job as it stands."""


@dataclass(frozen=True)
class Job:
    id: str
    state: str
    created: str
    started: str | None
    finished: str | None
    prompt: str
    seed: int
    width: int
    height: int
    steps: int
    model: str
    step: int
    image: str | None
    error: str | None
    cancel_requested: float | None
    token: str | None
    idempotency_key: str | None
    planned_image: str | None = None  # the image id the worker means to use, for cleaning up after a crash

    def same_request(self, request: Request, model: str, seed_given: bool = True) -> bool:
        return (self.prompt, self.width, self.height, self.steps, self.model) == (
            request.prompt, request.width, request.height, request.steps, model,
        ) and (not seed_given or self.seed == request.seed)  # fmt: skip


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def submit(
    request: Request, model: str, token: str | None = None, key: str | None = None, seed_given: bool = True
) -> tuple[Job, bool]:
    """Queues a job. Returns it, and whether it's new: the same key from the same token returns the first.

    With `seed_given` false, the request's seed was picked at random, so a retry's differing seed means nothing.
    """
    with db.session() as connection:
        connection.execute("BEGIN IMMEDIATE")  # the count and the insert have to be one step
        if key is not None:
            row = connection.execute(
                f"SELECT {COLUMNS} FROM jobs WHERE token IS ? AND idempotency_key = ?",  # nosec B608  # our own columns
                (token, key),
            ).fetchone()
            if row:
                existing = Job(*row)
                if not existing.same_request(request, model, seed_given):
                    raise Conflict("that Idempotency-Key was already used for a different request")
                return existing, False
        waiting = connection.execute("SELECT count(*) FROM jobs WHERE state = ?", (QUEUED,)).fetchone()[0]
        if waiting >= MAX_QUEUED:
            raise QueueFull(f"the queue is full, with {waiting} jobs waiting")
        job = Job(
            secrets.token_hex(6), QUEUED, _now(), None, None, request.prompt, request.seed, request.width,
            request.height, request.steps, model, 0, None, None, None, token, key,
        )  # fmt: skip
        marks = ", ".join("?" * 18)
        connection.execute(
            f"INSERT INTO jobs ({COLUMNS}) VALUES ({marks})",  # nosec B608  # our own columns and question marks
            (job.id, job.state, job.created, job.started, job.finished, job.prompt, job.seed, job.width, job.height,
             job.steps, job.model, job.step, job.image, job.error, job.cancel_requested, job.token, job.idempotency_key,
             job.planned_image),
        )  # fmt: skip
    return job, True


def find_by_key(token: str | None, key: str) -> Job | None:
    with db.session() as connection:
        row = connection.execute(
            f"SELECT {COLUMNS} FROM jobs WHERE token IS ? AND idempotency_key = ?",  # nosec B608  # our own columns
            (token, key),
        ).fetchone()
    return Job(*row) if row else None


def get(job_id: str) -> Job | None:
    with db.session() as connection:
        row = connection.execute(f"SELECT {COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()  # nosec B608
    return Job(*row) if row else None


def recent(limit: int = 50) -> list[Job]:
    """What's running and waiting, then the most recent others."""
    with db.session() as connection:
        rows = connection.execute(
            f"""SELECT {COLUMNS} FROM jobs ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
            CASE WHEN state IN ('running', 'queued') THEN created END ASC, created DESC, rowid DESC LIMIT ?""",  # nosec B608
            (limit,),
        ).fetchall()
    return [Job(*row) for row in rows]


def counts() -> dict[str, int]:
    with db.session() as connection:
        found = dict(connection.execute("SELECT state, count(*) FROM jobs GROUP BY state").fetchall())
    return {state: found.get(state, 0) for state in (QUEUED, RUNNING, DONE, FAILED, CANCELLED)}


def next_queued() -> Job | None:
    with db.session() as connection:
        row = connection.execute(
            f"SELECT {COLUMNS} FROM jobs WHERE state = ? ORDER BY created, rowid LIMIT 1",  # nosec B608
            (QUEUED,),
        ).fetchone()
    return Job(*row) if row else None


def claim(job_id: str, planned_image: str | None = None) -> Job | None:
    """Moves a job from queued to running. None means it was cancelled first."""
    with db.session() as connection:
        changed = connection.execute(
            "UPDATE jobs SET state = ?, started = ?, planned_image = ? WHERE id = ? AND state = ?",
            (RUNNING, _now(), planned_image, job_id, QUEUED),
        ).rowcount
    return get(job_id) if changed else None


def set_step(job_id: str, step: int) -> None:
    with db.session() as connection:
        connection.execute("UPDATE jobs SET step = ? WHERE id = ?", (step, job_id))


def cancel_was_requested(job_id: str) -> bool:
    with db.session() as connection:
        row = connection.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row[0] is not None)


def _finish(job_id: str, state: str, image: str | None = None, error: str | None = None) -> None:
    with db.session() as connection:
        connection.execute(
            "UPDATE jobs SET state = ?, finished = ?, image = ?, error = ? WHERE id = ? AND state = ?",
            (state, _now(), image, error, job_id, RUNNING),
        )


def finish_in(connection: sqlite3.Connection, job_id: str, image: str) -> None:
    """Marks a job done inside a transaction that's also recording its image."""
    connection.execute(
        "UPDATE jobs SET state = ?, finished = ?, image = ?, error = NULL, planned_image = NULL WHERE id = ? AND state = ?",
        (DONE, _now(), image, job_id, RUNNING),
    )


def fail(job_id: str, error: str) -> None:
    _finish(job_id, FAILED, error=error)


def mark_cancelled(job_id: str) -> None:
    _finish(job_id, CANCELLED)


def cancel(job_id: str) -> Job | None:
    """Stops a job: a waiting one at once, and a running one at its next step. None if there's no such job."""
    now = time.time()
    with db.session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        if row[0] in FINISHED:
            raise Conflict(f"that job is already {row[0]}")
        if row[0] == QUEUED:
            connection.execute(
                "UPDATE jobs SET state = ?, finished = ?, cancel_requested = ? WHERE id = ?",
                (CANCELLED, _now(), now, job_id),
            )
        else:
            connection.execute(
                "UPDATE jobs SET cancel_requested = COALESCE(cancel_requested, ?) WHERE id = ?", (now, job_id)
            )
    return get(job_id)


def stuck_in_cancel(grace_seconds: float) -> bool:
    """Whether a running job has ignored a cancel for this long, as one in its final decode can."""
    with db.session() as connection:
        row = connection.execute(
            "SELECT 1 FROM jobs WHERE state = ? AND cancel_requested IS NOT NULL AND cancel_requested < ? LIMIT 1",
            (RUNNING, time.time() - grace_seconds),
        ).fetchone()
    return row is not None


def recover_abandoned(reason: str = "the worker stopped while this job was running") -> list[str]:
    """Ends any job still marked running. Returns their ids.

    Call this only while holding the GPU, which proves no worker is running them. Otherwise, a live worker's job
    would be marked failed while it carries on.
    """
    with db.session() as connection:
        connection.execute("BEGIN IMMEDIATE")  # the read and the writes have to be one step
        rows = connection.execute("SELECT id, cancel_requested FROM jobs WHERE state = ?", (RUNNING,)).fetchall()
        for job_id, cancelled in rows:
            state, error = (CANCELLED, None) if cancelled is not None else (FAILED, reason)
            connection.execute(
                "UPDATE jobs SET state = ?, finished = ?, error = ? WHERE id = ? AND state = ?",
                (state, _now(), error, job_id, RUNNING),
            )
    return [row[0] for row in rows]


def plans_left_behind() -> list[tuple[str, str]]:
    """(job, image id) for jobs that ended without their image, whose file a crash may have left."""
    with db.session() as connection:
        rows = connection.execute(
            "SELECT id, planned_image FROM jobs WHERE planned_image IS NOT NULL AND state IN (?, ?)", (FAILED, CANCELLED)
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def forget_plan(job_id: str) -> None:
    with db.session() as connection:
        connection.execute("UPDATE jobs SET planned_image = NULL WHERE id = ?", (job_id,))


def prune(days: int = KEEP_FINISHED_DAYS) -> None:
    """Forgets finished jobs after a while, and with them the prompts of ones that never made an image."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.session() as connection:
        connection.execute("DELETE FROM jobs WHERE state IN (?, ?, ?) AND finished < ?", (*FINISHED, cutoff))

