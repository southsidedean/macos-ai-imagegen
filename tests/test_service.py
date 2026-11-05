"""The launchd agent and the power assertion. launchctl itself is run by hand, in scripts/real-engine.sh."""

from __future__ import annotations

import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from mia import catalog, cli, jobs, power, service, tokens
from mia.engine import MiaError, Request
from mia.fake_engine import FakeEngine
from mia.worker import Worker


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "mia"))
    monkeypatch.setenv("MIA_ENGINE", "fake")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.delenv("MIA_MODELS", raising=False)
    monkeypatch.delenv("MIA_SERVICE_LABEL", raising=False)


class Launchctl:
    """Stands in for launchctl, because a test must never change the real services on a Mac."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.answers: dict[str, list[subprocess.CompletedProcess[str]]] = {}
        monkeypatch.setattr(service, "_launchctl", self)
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def __call__(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        waiting = self.answers.get(arguments[0])
        return waiting.pop(0) if waiting else self.result(0)

    @staticmethod
    def result(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], code, out, err)

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture
def launchctl(monkeypatch: pytest.MonkeyPatch) -> Launchctl:
    return Launchctl(monkeypatch)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def written() -> dict[str, Any]:
    with service.plist_file().open("rb") as file:
        return dict(plistlib.load(file))


def test_the_agent_runs_this_python_and_comes_back_if_it_dies(launchctl: Launchctl) -> None:
    port = free_port()
    service.install(port)
    agent = written()
    assert agent["Label"] == "local.mia.serve"
    assert agent["ProgramArguments"] == [sys.executable, "-m", "mia", "serve", "--port", str(port)]
    assert agent["RunAtLoad"] is True and agent["KeepAlive"] is True
    assert agent["LimitLoadToSessionType"] == "Aqua"
    assert agent["Umask"] == 0o077  # the log and anything else it makes are private
    assert agent["ExitTimeOut"] > 30  # longer than mia serve waits for its worker, or launchd would kill it first
    assert service.plist_file().name == "local.mia.serve.plist"
    assert not list(service.plist_file().parent.glob("*.partial"))


def test_the_agent_holds_no_token_or_prompt_and_stays_on_this_mac(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch) -> None:
    issued = tokens.create("real")
    monkeypatch.setenv("MIA_TOKEN", issued)
    service.install(free_port())
    text = service.plist_file().read_text()
    assert issued not in text and "MIA_TOKEN" not in text
    assert "--host" not in text


def test_the_agent_uses_the_same_mia_as_the_shell_that_installed_it(launchctl: Launchctl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_MODELS", str(tmp_path / "external" / ".." / "models"))
    service.install(free_port())
    agent = written()
    assert agent["EnvironmentVariables"] == {"MIA_HOME": str((tmp_path / "mia").resolve()), "MIA_MODELS": str((tmp_path / "models").resolve())}
    assert agent["WorkingDirectory"] == str(tmp_path / "mia")
    assert agent["StandardErrorPath"] == str(tmp_path / "mia" / "logs" / "serve.log")
    assert (tmp_path / "mia" / "logs").stat().st_mode & 0o077 == 0


def test_settings_it_has_no_use_for_stay_behind(launchctl: Launchctl) -> None:
    service.install(free_port())
    assert set(written()["EnvironmentVariables"]) == {"MIA_HOME"}


@pytest.mark.skipif(not Path("/usr/bin/plutil").exists(), reason="plutil is part of macOS")
def test_macos_accepts_the_file_even_with_odd_characters(launchctl: Launchctl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "a & <b> folder"))
    service.install(free_port())
    subprocess.run(["/usr/bin/plutil", "-lint", str(service.plist_file())], check=True, capture_output=True)
    assert written()["EnvironmentVariables"]["MIA_HOME"].endswith("a & <b> folder")


def test_installing_replaces_whats_there_in_order(launchctl: Launchctl) -> None:
    service.install(free_port())
    assert launchctl.verbs() == ["bootout", "bootstrap"]
    assert launchctl.calls[0][1] == f"gui/{os.getuid()}/local.mia.serve"
    assert launchctl.calls[1] == ("bootstrap", f"gui/{os.getuid()}", str(service.plist_file()))


def test_nothing_being_there_to_replace_is_fine(launchctl: Launchctl) -> None:
    launchctl.answers["bootout"] = [Launchctl.result(3, err="Boot-out failed: 3: No such process")]
    service.install(free_port())
    assert service.plist_file().exists()


def test_a_real_failure_to_stop_the_old_one_stops_the_install(launchctl: Launchctl) -> None:
    launchctl.answers["bootout"] = [Launchctl.result(1, err="Boot-out failed: 1: Operation not permitted")]
    with pytest.raises(MiaError, match="Operation not permitted"):
        service.install(free_port())
    assert not service.plist_file().exists() and launchctl.verbs() == ["bootout"]


def test_starting_is_retried_while_the_old_one_finishes_going(launchctl: Launchctl) -> None:
    launchctl.answers["bootstrap"] = [Launchctl.result(5, err="Bootstrap failed: 5: Input/output error")] * 2
    service.install(free_port())
    assert launchctl.verbs() == ["bootout", "bootstrap", "bootstrap", "bootstrap"]


def test_it_says_so_if_starting_never_works(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "START_WAIT_SECONDS", 0)
    launchctl.answers["bootstrap"] = [Launchctl.result(125, err="Domain does not support specified action")]
    with pytest.raises(MiaError, match="logged in"):
        service.install(free_port())


def test_a_port_in_use_is_refused_before_anything_is_installed(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "START_WAIT_SECONDS", 0)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(MiaError, match="in use"):
            service.install(int(taken.getsockname()[1]))
    assert not service.plist_file().exists() and "bootstrap" not in launchctl.verbs()


def test_uninstalling_removes_the_agent_and_leaves_everything_else(launchctl: Launchctl, tmp_path: Path) -> None:
    issued = tokens.create("real")
    service.install(free_port())
    launchctl.calls.clear()
    assert service.uninstall() is True
    assert launchctl.calls == [("bootout", f"gui/{os.getuid()}/local.mia.serve")]  # or it would run on, with no file
    assert not service.plist_file().exists()
    assert tokens.verify(issued) is not None and (tmp_path / "mia").exists()
    launchctl.answers["bootout"] = [Launchctl.result(3, err="Boot-out failed: 3: No such process")]
    assert service.uninstall() is False


def test_status_reports_what_launchd_and_the_server_say(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch) -> None:
    assert service.status() == service.Status(False, None, None, False)
    assert launchctl.calls == []  # with nothing installed, there's nothing to ask
    service.install(8765)
    monkeypatch.setattr(service, "_answers", lambda port: port == 8765)
    launchctl.answers["print"] = [Launchctl.result(0, "local.mia.serve = {\n\tstate = running\n\tpid = 4242\n\tinherited = {\n}\n")]
    assert service.status() == service.Status(True, 4242, 8765, True)
    launchctl.answers["print"] = [Launchctl.result(113, err="Could not find service")]
    monkeypatch.setattr(service, "_answers", lambda _port: False)
    assert service.status() == service.Status(True, None, 8765, False)


def test_the_commands(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    port = free_port()
    assert cli.main(["service"]) == 1
    assert "not installed" in capsys.readouterr().out
    assert cli.main(["service", "install", "--port", str(port)]) == 0
    assert str(port) in capsys.readouterr().out
    monkeypatch.setattr(service, "_answers", lambda _port: False)
    assert cli.main(["service", "status"]) == 1  # a script can tell that it isn't working
    assert "not answering" in capsys.readouterr().out
    assert cli.main(["service", "uninstall"]) == 0
    assert "still there" in capsys.readouterr().out
    assert cli.main(["service", "uninstall"]) == 0
    assert "wasn't installed" in capsys.readouterr().out


def test_mia_service_refuses_anywhere_but_a_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(MiaError, match="macOS only"):
        service.install(free_port())
    assert not service.plist_file().exists()


# The power assertion.


def _children() -> list[str]:
    done = subprocess.run(["ps", "-o", "command=", "--ppid", str(os.getpid())], capture_output=True, text=True, check=False)
    if done.returncode != 0:  # macOS's ps has no --ppid
        everything = subprocess.run(["ps", "-A", "-o", "ppid=,command="], capture_output=True, text=True, check=True).stdout
        return [line.split(None, 1)[1] for line in everything.splitlines() if line.split(None, 1)[0] == str(os.getpid())]
    return done.stdout.splitlines()


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A program that waits the way caffeinate does, on any machine, with a name these tests can find."""
    program = tmp_path / "stand-in-caffeinate"
    program.write_text("#!/bin/sh\nexec sleep 300\n")
    program.chmod(0o755)
    monkeypatch.setattr(power, "CAFFEINATE", str(program))
    return program.name


def held(name: str) -> bool:
    return any(name in line or "sleep 300" in line for line in _children())


def test_the_mac_stays_awake_for_a_job_and_no_longer(stand_in: str) -> None:
    seen: list[bool] = []

    class Watching(FakeEngine):
        def generate(self, *arguments: Any) -> str:
            seen.append(held(stand_in))
            return super().generate(*arguments)

    job, _ = jobs.submit(Request("a fox", 1, 512, 512, 2), catalog.DEFAULT.id)
    assert not held(stand_in)
    Worker(lambda _model: Watching()).step()
    assert seen == [True]
    assert not held(stand_in)
    finished = jobs.get(job.id)
    assert finished is not None and finished.state == jobs.DONE


def test_loading_a_model_counts_as_part_of_the_job(stand_in: str) -> None:
    seen: list[bool] = []

    def loader(_model: catalog.Model) -> FakeEngine:
        seen.append(held(stand_in))
        return FakeEngine()

    jobs.submit(Request("a fox", 1, 512, 512, 1), catalog.DEFAULT.id)
    Worker(loader).step()
    assert seen == [True]


def test_an_idle_worker_doesnt_hold_the_mac_awake(stand_in: str) -> None:
    assert Worker(lambda _model: FakeEngine()).step() is False
    assert not held(stand_in)


def test_a_failed_job_lets_the_mac_sleep(stand_in: str) -> None:
    class Failing(FakeEngine):
        def generate(self, *_arguments: Any) -> str:
            raise RuntimeError("the engine broke")

    job, _ = jobs.submit(Request("a fox", 1, 512, 512, 1), catalog.DEFAULT.id)
    Worker(lambda _model: Failing()).step()
    assert not held(stand_in)
    assert jobs.get(job.id).state == jobs.FAILED  # type: ignore[union-attr]


def test_an_interrupt_lets_the_mac_sleep(stand_in: str) -> None:
    with pytest.raises(KeyboardInterrupt), power.awake():
        assert held(stand_in)
        raise KeyboardInterrupt
    assert not held(stand_in)


def test_a_job_still_runs_if_the_mac_cant_be_kept_awake(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(power, "CAFFEINATE", "/nothing/here")
    job, _ = jobs.submit(Request("a fox", 1, 512, 512, 1), catalog.DEFAULT.id)
    Worker(lambda _model: FakeEngine()).step()
    assert jobs.get(job.id).state == jobs.DONE  # type: ignore[union-attr]
    assert "awake" in capsys.readouterr().err


@pytest.mark.skipif(not shutil.which("caffeinate"), reason="caffeinate is part of macOS")
def test_real_caffeinate_ends_when_the_worker_is_killed() -> None:
    # The promise that a crash can't leave a Mac that never sleeps, with the real program.
    code = "import time; from mia import power\nwith power.awake():\n    print('in', flush=True)\n    time.sleep(60)\n"
    worker = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert worker.stdout is not None
    assert worker.stdout.readline().strip() == "in"
    needle = f"caffeinate -i -w {worker.pid}"

    def running() -> bool:
        return needle in subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True, check=True).stdout

    assert running()
    worker.kill()
    worker.wait()
    deadline = time.monotonic() + 10
    while running() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not running()
