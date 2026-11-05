"""Runs mia serve as a launchd user agent, so it starts at login and comes back if it dies.

One agent: mia serve looks after its worker, because stopping a stuck job and recording why takes more than launchd's
restarts can do. A user agent needs someone logged in, and in return it runs where the GPU is available.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mia import paths
from mia.engine import MiaError

LABEL = "local.mia.serve"
LAUNCHCTL = "/bin/launchctl"
# What the agent can't otherwise know, as it doesn't see this shell's settings.
PASSED_ON_PATHS = ("MIA_HOME", "MIA_MODELS")
PASSED_ON = ("MIA_IDLE_SECONDS",)
THROTTLE_SECONDS = 10  # launchd's own minimum, between restarts of something that keeps failing
EXIT_SECONDS = 60  # longer than mia serve needs to stop its worker, as after launchd's default 20 it kills it
START_WAIT_SECONDS = 70  # bootout can return before the old one has gone, and then bootstrap fails for a while
# launchctl's own words for "this isn't loaded", in the two forms it uses.
NOT_LOADED = re.compile(r"could not find service|no such process", re.IGNORECASE)
MAX_LOG_BYTES = 10 * 1024**2


def _label() -> str:
    """Another label lets scripts/real-engine.sh test a real agent without touching one that's installed."""
    return os.environ.get("MIA_SERVICE_LABEL", LABEL)


def plist_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label()}.plist"


def log_file() -> Path:
    return paths.home_folder() / "logs" / "serve.log"


def _target() -> str:
    return f"gui/{os.getuid()}/{_label()}"


def rotate_log() -> None:
    """Keeps the service's log to about twice MAX_LOG_BYTES, and leaves any other log alone.

    launchd opens the log once and holds it, so renaming it would keep it growing under the new name. It opens it to
    append, so copying it and then emptying it is safe, though a line written in between is lost. A problem here isn't
    worth stopping anything for.
    """
    log = log_file()
    try:
        if log.stat().st_size <= MAX_LOG_BYTES or not os.path.samestat(os.fstat(2), log.stat()):
            return
        partial = log.with_name(f"{log.name}.partial")
        shutil.copyfile(log, partial)
        partial.chmod(0o600)
        partial.replace(log.with_name(f"{log.name}.1"))
        os.truncate(log, 0)
    except OSError:
        pass


def describe(port: int) -> dict[str, Any]:
    """What launchd is told. It holds no token, and mia's logs hold no prompts."""
    log = str(log_file())
    environment = {name: str(Path(os.environ[name]).expanduser().resolve()) for name in PASSED_ON_PATHS if os.environ.get(name)}
    environment |= {name: os.environ[name] for name in PASSED_ON if os.environ.get(name)}
    return {
        "Label": _label(),
        # This Python, so the agent runs the install that set it up. Moving that folder means installing again.
        "ProgramArguments": [sys.executable, "-m", "mia", "serve", "--port", str(port)],
        "EnvironmentVariables": environment,
        "WorkingDirectory": str(paths.home_folder()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_SECONDS,
        "ExitTimeOut": EXIT_SECONDS,
        "LimitLoadToSessionType": "Aqua",  # only a logged-in desktop session, which is where the GPU is
        "Umask": 0o077,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    if sys.platform != "darwin":
        raise MiaError("mia service runs on macOS only")
    return subprocess.run([LAUNCHCTL, *arguments], capture_output=True, text=True, check=False, timeout=EXIT_SECONDS + 30)


def _unload() -> None:
    done = _launchctl("bootout", _target())
    if done.returncode != 0 and not NOT_LOADED.search(done.stderr + done.stdout):
        raise MiaError(f"launchctl couldn't stop the service: {done.stderr.strip()}")


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _wait_for_port(port: int, deadline: float) -> None:
    while not _port_is_free(port):
        if time.monotonic() >= deadline:
            raise MiaError(f"port {port} is in use, perhaps by a mia serve you started yourself. Stop that first")
        time.sleep(1)


def _load(deadline: float) -> None:
    while True:
        done = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_file()))
        if done.returncode == 0:
            return
        if time.monotonic() >= deadline:
            raise MiaError(f"launchctl couldn't start the service: {done.stderr.strip()}. It needs someone logged in at the Mac")
        time.sleep(1)


def install(port: int) -> Path:
    """Installs and starts the agent. Installing again replaces it, which stops any job it's running."""
    described = describe(port)  # first, so that nothing has changed if this can't work
    _unload()
    deadline = time.monotonic() + START_WAIT_SECONDS
    _wait_for_port(port, deadline)
    paths.make_private(paths.home_folder())
    paths.make_private(log_file().parent)
    target = plist_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    with partial.open("wb") as file:
        plistlib.dump(described, file)
    partial.replace(target)
    _load(deadline)
    return target


def _installed_port() -> int:
    if not plist_file().exists():
        raise MiaError("mia's service isn't installed: mia service install sets it up")
    port = _port_in_file()
    if port is None:
        raise MiaError("mia's service file looks wrong: mia service install replaces it")
    return port


def stop() -> None:
    """Stops it until the next login, or until start or restart. The file stays, so launchd loads it again at login."""
    _installed_port()
    _unload()


def start() -> None:
    """Starts it if it's stopped. Starting one that's already running is fine, and changes nothing."""
    port = _installed_port()
    if _launchctl("print", _target()).returncode == 0:  # loaded, so launchd already keeps it running
        return
    deadline = time.monotonic() + START_WAIT_SECONDS
    _wait_for_port(port, deadline)
    _load(deadline)


def restart() -> None:
    """Runs the code that's installed now, which is what upgrading needs. It stops any job that's running."""
    port = _installed_port()
    _unload()
    deadline = time.monotonic() + START_WAIT_SECONDS
    _wait_for_port(port, deadline)
    _load(deadline)


def uninstall() -> bool:
    """Stops the agent and removes it. mia's images, history, and tokens stay. Returns whether it was there at all."""
    was_there = plist_file().exists()
    _unload()
    plist_file().unlink(missing_ok=True)
    return was_there


@dataclass(frozen=True)
class Status:
    installed: bool
    pid: int | None
    port: int | None
    version: str | None  # what the running server says it is, or None if it isn't answering

    @property
    def answering(self) -> bool:
        return self.version is not None


def _port_in_file() -> int | None:
    try:
        with plist_file().open("rb") as file:
            arguments = plistlib.load(file).get("ProgramArguments", [])
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return int(arguments[-1]) if arguments and str(arguments[-1]).isdigit() else None


def _pid() -> int | None:
    found = re.search(r"^\s*pid = (\d+)$", _launchctl("print", _target()).stdout, re.MULTILINE)
    return int(found[1]) if found else None


def status() -> Status:
    if not plist_file().exists():
        return Status(False, None, None, None)
    port = _port_in_file()
    return Status(True, _pid(), port, _running_version(port) if port is not None else None)


def _running_version(port: int) -> str | None:
    # The fixed loopback address over plain HTTP, never a proxy: only this Mac's own server is being asked.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/v1/health", timeout=3) as answer:  # nosec B310
            found = json.load(answer).get("version")
    except (OSError, ValueError, AttributeError, urllib.error.URLError):
        return None
    return str(found) if found else None
