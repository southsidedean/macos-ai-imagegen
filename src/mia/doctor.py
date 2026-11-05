"""mia doctor: is this machine ready to run mia? It only reads, and it needs no AI.

It never imports the ML packages, which are large and slow to load, so it can still run and say
what's wrong when they're missing or broken. Facts are gathered once and judged separately, so the
tests can try any machine.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mia import __version__, paths, remote, service
from mia.engine import MiaError

GB = 1024**3
# What the engine installs on: mflux needs sentencepiece, which has no wheels for Python 3.15 yet.
PYTHONS = ((3, 12), (3, 13), (3, 14))
# FLUX.2 klein 4B at 4-bit needs 4.6 GB to download. Below this, even the default can't fit.
DISK_NEEDED_GB = 6
# Room for the default model plus another, and for the images they make.
DISK_COMFORTABLE_GB = 30
# Klein 4B's whole process peaked at about 8 GB at 1024 by 1024. The true minimum hasn't been measured
# on a smaller Mac, so this only warns.
MEMORY_COMFORTABLE_GB = 16
ENGINES = ("mflux", "mlx")

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"


@dataclass(frozen=True)
class Facts:
    system: str
    machine: str
    python: tuple[int, int]
    memory_gb: float | None
    folder: Path
    disk_free_gb: float | None
    engines: dict[str, bool]
    models: int
    service: service.Status | None = None  # None where there's no launchd to ask
    remote: remote.State | None = None  # None when it's off


@dataclass(frozen=True)
class Finding:
    status: str
    name: str
    detail: str


def _memory_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / GB
    except (AttributeError, ValueError, OSError):
        return None


def _disk_free_gb(path: Path) -> float | None:
    # mia's folder may not exist yet, and doctor changes nothing, so ask about the nearest parent.
    for candidate in (path, *path.parents):
        if candidate.exists():
            try:
                return shutil.disk_usage(candidate).free / GB
            except OSError:
                return None
    return None


def _count_models(models: Path) -> int:
    try:
        return sum(1 for entry in models.iterdir() if entry.is_dir())
    except OSError:
        return 0


def _service_state() -> service.Status | None:
    if sys.platform != "darwin":
        return None
    try:
        return service.status()
    except (MiaError, OSError, subprocess.SubprocessError):  # it's only one finding: doctor has to get to the rest
        return None


def _remote_state() -> remote.State | None:
    try:
        return remote.state()
    except MiaError:
        return remote.State(remote.Settings((), ()), None)


def gather() -> Facts:
    home = paths.home_folder()
    return Facts(
        system=platform.system(),
        machine=platform.machine(),
        python=(sys.version_info.major, sys.version_info.minor),
        memory_gb=_memory_gb(),
        folder=home,
        disk_free_gb=_disk_free_gb(home),
        engines={name: importlib.util.find_spec(name) is not None for name in ENGINES},
        models=_count_models(paths.models_folder()),
        service=_service_state(),
        remote=_remote_state(),
    )


def _machine(facts: Facts) -> Finding:
    if facts.system == "Darwin" and facts.machine == "arm64":
        return Finding(PASS, "Apple Silicon Mac", f"macOS {platform.mac_ver()[0]}".strip())
    return Finding(FAIL, "Apple Silicon Mac", f"this is {facts.system} on {facts.machine}; mia's engines need Metal")


def _python(facts: Facts) -> Finding:
    version = ".".join(map(str, facts.python))
    if facts.python in PYTHONS:
        return Finding(PASS, "Python", version)
    wanted = " or ".join(".".join(map(str, p)) for p in PYTHONS)
    return Finding(WARN, "Python", f"{version}; the image engine installs only on {wanted}")


def _memory(facts: Facts) -> Finding:
    if facts.memory_gb is None:
        return Finding(WARN, "Memory", "couldn't read how much this machine has")
    size = f"{facts.memory_gb:.0f} GB"
    if facts.memory_gb < MEMORY_COMFORTABLE_GB:
        return Finding(WARN, "Memory", f"{size}; a 1024 by 1024 image used about 8 GB in testing, with more for other models")
    return Finding(PASS, "Memory", size)


def _disk(facts: Facts) -> Finding:
    if facts.disk_free_gb is None:
        return Finding(WARN, "Disk", f"couldn't read the free space for {facts.folder}")
    free = f"{facts.disk_free_gb:.0f} GB free"
    if facts.disk_free_gb < DISK_NEEDED_GB:
        return Finding(FAIL, "Disk", f"{free}; the default model needs at least {DISK_NEEDED_GB} GB")
    if facts.disk_free_gb < DISK_COMFORTABLE_GB:
        return Finding(WARN, "Disk", f"{free}; {DISK_COMFORTABLE_GB} GB leaves room for more models and images")
    return Finding(PASS, "Disk", free)


def _engines(facts: Facts) -> Finding:
    missing = [name for name, found in facts.engines.items() if not found]
    if missing:
        return Finding(INFO, "Image engine", f"not installed ({', '.join(missing)}); run uv sync --extra engine")
    return Finding(PASS, "Image engine", "installed")


def _service(state: service.Status) -> Finding:
    if not state.installed:
        return Finding(INFO, "Service", "not installed; mia service install keeps mia running")
    if not state.answering:
        return Finding(WARN, "Service", f"installed, but not answering on port {state.port}; see mia service status")
    if state.version != __version__:
        return Finding(WARN, "Service", f"running mia {state.version}, and {__version__} is installed; run mia service restart")
    return Finding(PASS, "Service", f"running on port {state.port}")


def _remote(state: remote.State | None) -> Finding:
    if state is None:
        return Finding(INFO, "Remote access", "off; only this Mac can reach mia")
    if state.certificate_ends is None:
        return Finding(WARN, "Remote access", "on, but it has no working certificate or settings; run mia remote on")
    if state.certificate_ends <= datetime.now(UTC):
        return Finding(WARN, "Remote access", "its certificate has run out; run mia service restart to renew it")
    return Finding(PASS, "Remote access", f"on, at {', '.join(state.settings.urls)}")


def findings(facts: Facts) -> list[Finding]:
    return [
        _machine(facts),
        _python(facts),
        _memory(facts),
        _disk(facts),
        _engines(facts),
        Finding(INFO, "mia's folder", f"{facts.folder} ({'found' if facts.folder.exists() else 'not made yet'})"),
        Finding(INFO, "Models", f"{facts.models} in the models folder"),
        *([_service(facts.service)] if facts.service else []),
        _remote(facts.remote),
    ]


def run() -> int:
    """Prints the findings. Exits 1 only for a problem that stops mia from working."""
    print(f"mia {__version__} doctor")
    results = findings(gather())
    for result in results:
        print(f"{result.status:<5} {result.name}: {result.detail}")
    return 1 if any(result.status == FAIL for result in results) else 0
