"""Keeps a worker running beside the server, and records what happened when one dies.

This is how the API stays up through a worker crash or an out-of-memory kill. It's a thread in the
server's process, which never imports an ML package: the worker is always a separate process.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time

from mia import gpu, jobs

CHECK_SECONDS = 1.0
FIRST_BACKOFF, MAX_BACKOFF = 1.0, 60.0
HEALTHY_AFTER = 60.0  # a worker that lasted this long gets a fresh start on its backoff
# A cancel takes effect between steps, so one in the final decode has to wait for it to finish. After
# this long, the only way left to stop the job is to stop the worker.
CANCEL_GRACE = 60.0
# Long enough for the slowest step or decode on a slow Mac, so this only stops an engine that has hung. It's measured
# on the monotonic clock, which stops while a Mac sleeps, so closing the lid doesn't count against a job.
STALL_SECONDS = 1800.0
STALLED = "the job made no progress for 30 minutes, so mia restarted the worker"
STOP_WAIT = 30.0


def _log(message: str) -> None:
    """A closed stderr, as with mia serve | head, mustn't stop the thread that looks after the worker."""
    with contextlib.suppress(OSError, ValueError):
        print(message, file=sys.stderr, flush=True)


def recover_if_free(reason: str | None = None) -> None:
    """Ends jobs marked running, but only if holding the GPU proves no worker is running them.

    If another process has the GPU, it may be a live worker, and its job isn't abandoned: whichever worker
    next starts does this itself.
    """
    try:
        with gpu.hold(briefly=True):
            if reason is None:
                jobs.recover_abandoned()
            else:
                jobs.recover_abandoned(reason)
    except gpu.Busy:
        pass


class Supervisor:
    def __init__(self) -> None:
        self._stopping = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread = threading.Thread(target=self._run, name="mia-supervisor", daemon=True)
        self._started_at = 0.0
        self._not_before = 0.0
        self._backoff = FIRST_BACKOFF
        self._progress: tuple[str, int] | None = None
        self._progress_since = 0.0
        self._ended_because: str | None = None

    def start(self) -> None:
        self._thread.start()

    def running(self) -> bool:
        """Whether a worker process is up, and this thread is still looking after it."""
        return self._thread.is_alive() and self._process is not None and self._process.poll() is None

    def _spawn(self) -> subprocess.Popen[bytes]:
        return subprocess.Popen([sys.executable, "-m", "mia", "worker", "--parent", str(os.getpid())])

    def _run(self) -> None:
        # Anything can fail for a moment, such as a locked database or too many open files. If this thread died,
        # the API would look healthy with no worker behind it, so it logs the problem and tries again.
        while not self._stopping.is_set():
            try:
                self._check()
            except Exception as problem:
                _log(f"the supervisor hit a problem, and will try again: {type(problem).__name__}")
            self._stopping.wait(CHECK_SECONDS)

    def _check(self) -> None:
        if self._process is None:
            if time.monotonic() >= self._not_before:
                self._started_at = time.monotonic()
                self._process = self._spawn()
            return
        code = self._process.poll()
        if code is None:
            if jobs.stuck_in_cancel(CANCEL_GRACE):
                self._process.kill()
            elif self._stalled():
                self._ended_because = STALLED
                self._process.kill()
            return
        # Recover before forgetting the process: if this fails, the next check comes back here and tries again.
        recover_if_free(self._ended_because)
        self._process = None
        self._progress = self._ended_because = None
        if time.monotonic() - self._started_at > HEALTHY_AFTER:
            self._backoff = FIRST_BACKOFF
        if code != 0:  # zero is the worker asking for a new process, for another model
            _log(f"the worker stopped unexpectedly, with code {code}; restarting it")
            self._not_before = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    def _stalled(self) -> bool:
        progress = jobs.running_progress()
        if progress != self._progress:
            self._progress, self._progress_since = progress, time.monotonic()
            return False
        return progress is not None and time.monotonic() - self._progress_since > STALL_SECONDS

    def stop(self) -> None:
        self._stopping.set()
        self._thread.join(CHECK_SECONDS * 5)  # first, so it can't start a new worker after this looks
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(STOP_WAIT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        recover_if_free("mia was stopped while this job was running")
