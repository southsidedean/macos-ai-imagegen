"""Keeps the Mac from going to sleep through idle while a job runs, and only then, so an idle Mac can still sleep.

caffeinate rather than a call into IOKit: it's part of macOS, and -w ends it when the worker dies, however it dies, so a
crash can't leave a Mac that never sleeps.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager

CAFFEINATE = "/usr/bin/caffeinate"
STOP_WAIT = 5


@contextmanager
def awake() -> Iterator[None]:
    try:
        process = subprocess.Popen(
            [CAFFEINATE, "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Its own session, so Control-C in a terminal reaches the worker alone, which then ends this in its own time.
            start_new_session=True,
        )
    except OSError:  # not on a Mac, or it's been removed: a job can still run, and may just be cut off by sleep
        with contextlib.suppress(OSError, ValueError):
            print("couldn't keep the Mac awake for this job", file=sys.stderr, flush=True)
        yield
        return
    try:
        yield
    finally:
        process.terminate()
        try:
            process.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
