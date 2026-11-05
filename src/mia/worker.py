"""The only process that loads a model. It runs one job at a time, and may be killed at any moment.

Everything it knows is in the database, so a new worker picks up where a dead one left off, ending
whatever job that one was running. A model is kept loaded between jobs. Moving to another means a new
process, the sure way to hand back the old model's memory.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import sys
import time
import traceback
from collections.abc import Callable
from types import FrameType

from mia import catalog, gpu, history, jobs, paths
from mia.engine import Cancelled, Engine, MiaError, Request
from mia.generate import generate, load_engine, new_image_id

POLL_SECONDS = 0.5
RESTART_FOR_MODEL = 0  # an ordinary exit: whatever supervises the worker starts a new one
STOPPED = "mia was stopped while this job was running"
IMAGE_ID = re.compile(r"[0-9a-f]{12}")
PARTIAL = re.compile(r"\.[0-9a-f]{12}\.partial\.png")
PRUNE_SECONDS = 3600


class NewModelNeeded(Exception):
    """The next job needs a different model from the one loaded."""


class Worker:
    def __init__(self, loader: Callable[[catalog.Model], Engine] = load_engine) -> None:
        self._loader = loader
        self._engine: Engine | None = None
        self._model: str | None = None
        self.stopping = False

    def step(self) -> bool:
        """Runs the next job, if there is one. Returns whether it did."""
        waiting = jobs.next_queued()
        if waiting is None:
            return False
        if self._model is not None and self._model != waiting.model:
            raise NewModelNeeded
        image_id = new_image_id()
        job = jobs.claim(waiting.id, image_id)
        if job is None:
            return True  # cancelled in the moment since it was read
        try:
            model = catalog.find(job.model)
            if self._engine is None:
                self._engine = self._loader(model)
                self._model = job.model
            generate(
                Request(job.prompt, job.seed, job.width, job.height, job.steps),
                model,
                self._engine,
                progress=lambda step, _total: jobs.set_step(job.id, step),
                cancelled=lambda: jobs.cancel_was_requested(job.id),
                # In one transaction, so a crash can't leave a recorded image beside a failed job.
                then=lambda connection, record: jobs.finish_in(connection, job.id, record.id),
                image_id=image_id,
            )
        except Cancelled:
            if jobs.cancel_was_requested(job.id):
                jobs.mark_cancelled(job.id)
            else:
                # Nobody asked to cancel, so this was Ctrl-C or SIGTERM, which mflux's step loop catches and
                # turns into a stop of this one job. This worker still has to stop, and not take the next.
                jobs.fail(job.id, STOPPED)
                self.stopping = True
        except MiaError as problem:
            jobs.fail(job.id, str(problem))
        except Exception as crash:
            # Frames and the error's type only: an error's message can quote its input, here the prompt.
            _log(f"job {job.id} failed: {type(crash).__name__}\n{''.join(traceback.format_tb(crash.__traceback__))}")
            jobs.fail(job.id, f"the engine failed with {type(crash).__name__}; the worker's log has where")
        return True


def sweep_images() -> None:
    """Removes what a killed worker left in the images folder. Only with the GPU held, so nothing is making one.

    This never looks for pictures with no record, because the history can be lost or restored from a backup, and
    that would make every picture look like one. It removes partial files, which are never finished pictures, and
    the one picture a job that failed had planned to make, if it has no record. A file it can't remove is left: it
    mustn't stop the queue.
    """
    folder = paths.images_folder()
    leftovers = [path for path in folder.glob(".*.partial.png") if PARTIAL.fullmatch(path.name)]
    for job_id, image_id in jobs.plans_left_behind():
        if IMAGE_ID.fullmatch(image_id) and history.get(paths.history_file(), image_id) is None:
            leftovers.append(folder / f"{image_id}.png")
        jobs.forget_plan(job_id)
    for path in leftovers:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _log(f"couldn't remove a leftover image: {path.name}")


def _log(message: str) -> None:
    """A closed stderr, as with mia serve | head, mustn't turn a problem with one job into a dead worker."""
    with contextlib.suppress(OSError, ValueError):
        print(message, file=sys.stderr, flush=True)


def run(parent: int | None = None) -> int:
    """Runs until stopped. `parent` is the supervisor: if it's gone, nothing would restart this."""
    worker = Worker()

    def stop(_signal: int, _frame: FrameType | None) -> None:
        if not worker.stopping:  # only the first: a second mustn't interrupt the clean-up from the first
            worker.stopping = True
            raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def wanted() -> bool:
        return not worker.stopping and (parent is None or os.getppid() == parent)

    try:
        with gpu.hold(wait_while=wanted):
            try:
                # Holding the GPU proves no other worker is running, so anything marked running is abandoned.
                jobs.recover_abandoned()
                sweep_images()
                pruned = 0.0
                while wanted():
                    if time.monotonic() - pruned > PRUNE_SECONDS or pruned == 0.0:
                        jobs.prune()  # here, as a worker can run for months: the docs promise 30 days
                        pruned = time.monotonic()
                    if not worker.step():
                        time.sleep(POLL_SECONDS)
            except NewModelNeeded:
                return RESTART_FOR_MODEL
            except KeyboardInterrupt:
                jobs.recover_abandoned(STOPPED)  # still holding the GPU, so this can only be our own job
    except gpu.Busy:
        pass  # told to stop while still waiting for the GPU, with nothing to clean up
    except KeyboardInterrupt:
        pass  # the same, arriving during the wait
    return 0
