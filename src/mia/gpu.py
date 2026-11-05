"""One process at a time may use the GPU: a second loaded model would use up the Mac's memory.

The operating system drops a flock when its holder dies, however it dies, so a crash can't leave the
GPU marked as taken. Holding it also proves no worker is running, which is what makes it safe to end a job
marked as running. The file holds the holder's process id, for finding the worker.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from mia import paths
from mia.engine import MiaError

WAIT_SECONDS = 0.5


class Busy(MiaError):
    """Another process has the GPU."""


def _open() -> int:
    paths.make_private(paths.home_folder())
    return os.open(paths.home_folder() / "gpu.lock", os.O_RDWR | os.O_CREAT, 0o600)


@contextmanager
def hold(wait_while: Callable[[], bool] | None = None, briefly: bool = False) -> Iterator[None]:
    """Holds the GPU. With `wait_while`, waits for as long as it's true, and otherwise says it's busy.

    `briefly` is for a process that isn't going to use it, and leaves the file naming the last one that did.
    """
    descriptor = _open()
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if wait_while is None or not wait_while():
                    raise Busy(
                        "another mia process is using the GPU. If mia serve is running, send this through its API, or stop it first: "
                        "mia service stop, if it's running as a service"
                    ) from None
                time.sleep(WAIT_SECONDS)
        if not briefly:
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, str(os.getpid()).encode(), 0)
        yield
    finally:
        os.close(descriptor)  # which also drops the lock
