"""The launchd agent and the power assertion. launchctl itself is run by hand, in scripts/real-engine.sh."""

from __future__ import annotations

import os
import plistlib
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


REAL_CONFIRM = service._confirm_answering


class Launchctl:
    """Stands in for launchctl, because a test must never change the real services on a Mac."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.answers: dict[str, list[subprocess.CompletedProcess[str]]] = {}
        monkeypatch.setattr(service, "_launchctl", self)
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        # Nothing is really started here, so nothing could answer. The tests of that wait put it back.
        monkeypatch.setattr(service, "_confirm_answering", lambda _port: None)

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


def test_a_busy_port_leaves_the_running_service_and_its_job_alone(launchctl: Launchctl) -> None:
    service.install(free_port())
    launchctl.calls.clear()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(MiaError, match="in use"):
            service.install(int(taken.getsockname()[1]))
    assert "bootout" not in launchctl.verbs()


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_a_port_that_cant_be_is_refused_before_anything_stops(launchctl: Launchctl, port: int) -> None:
    with pytest.raises(MiaError, match="isn't a port"):
        service.install(port)
    assert launchctl.calls == []


def test_the_service_on_its_own_port_can_be_replaced(launchctl: Launchctl) -> None:
    with socket.socket() as itself:
        itself.bind(("127.0.0.1", 0))
        itself.listen()
        port = int(itself.getsockname()[1])
        service.plist_file().parent.mkdir(parents=True)
        service.plist_file().write_bytes(plistlib.dumps({"ProgramArguments": ["python", "-m", "mia", "serve", "--port", str(port)]}))
        with pytest.raises(MiaError, match="in use"):  # it isn't loaded, so something else must hold the port
            launchctl.answers["print"] = [Launchctl.result(113, err="Could not find service")]
            service.install(port)
        assert launchctl.verbs() == ["print"]
        launchctl.calls.clear()
        launchctl.answers["print"] = [Launchctl.result(0)]
        itself.close()  # as the old one does once it's stopped, though the check above saw it busy
        service.install(port)
    assert launchctl.verbs()[-1] == "bootstrap"


@pytest.mark.parametrize("action", ["install", "restart", "start"])
def test_a_server_that_never_answers_is_reported_with_its_log(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    port = free_port()
    service.install(port)
    monkeypatch.setattr(service, "_confirm_answering", REAL_CONFIRM)
    monkeypatch.setattr(service, "ANSWER_WAIT_SECONDS", 0)
    monkeypatch.setattr(service, "_running_version", lambda _port: None)
    if action == "start":
        launchctl.answers["print"] = [Launchctl.result(113, err="Could not find service")]
    with pytest.raises(MiaError, match="isn't answering") as refused:
        service.install(port) if action == "install" else getattr(service, action)()
    assert str(service.log_file()) in str(refused.value)


def test_waiting_ends_as_soon_as_it_answers(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = [None, None, "1.2.3"]
    monkeypatch.setattr(service, "_confirm_answering", REAL_CONFIRM)
    monkeypatch.setattr(service, "_running_version", lambda _port: answers.pop(0))
    service.install(free_port())
    assert answers == []


@pytest.mark.parametrize("damage", [b"<?xml version='1.0'?><plist><dict>", b"<plist version='1.0'><array><string>x</string></array></plist>", b"\x00junk"])
def test_a_damaged_service_file_is_described_and_not_a_crash(launchctl: Launchctl, damage: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    service.plist_file().parent.mkdir(parents=True)
    service.plist_file().write_bytes(damage)
    assert service.installed_port() is None
    assert service.status() == service.Status(True, None, None, None)
    with pytest.raises(MiaError, match="looks wrong"):
        service.restart()
    assert not [call for call in launchctl.calls if call[0] == "bootout"]


def test_arguments_that_arent_a_list_dont_crash_it(launchctl: Launchctl) -> None:
    service.plist_file().parent.mkdir(parents=True)
    service.plist_file().write_bytes(plistlib.dumps({"ProgramArguments": "8765"}))
    assert service.installed_port() is None


def test_another_program_on_the_port_counts_as_not_answering() -> None:
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = int(other.getsockname()[1])

        def answer_nonsense() -> None:
            held, _ = other.accept()
            held.recv(1024)
            held.sendall(b"not http at all\r\n\r\n")
            held.close()

        thread = threading.Thread(target=answer_nonsense, daemon=True)
        thread.start()
        assert service._running_version(port) is None
        thread.join(5)


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
    assert service.status() == service.Status(False, None, None, None)
    assert launchctl.calls == []  # with nothing installed, there's nothing to ask
    port = free_port()  # 8765 would fail whenever someone is running mia on this Mac
    service.install(port)
    monkeypatch.setattr(service, "_running_version", lambda asked: "1.2.3" if asked == port else None)
    launchctl.answers["print"] = [Launchctl.result(0, "local.mia.serve = {\n\tstate = running\n\tpid = 4242\n\tinherited = {\n}\n")]
    assert service.status() == service.Status(True, 4242, port, "1.2.3")
    launchctl.answers["print"] = [Launchctl.result(113, err="Could not find service")]
    monkeypatch.setattr(service, "_running_version", lambda _port: None)
    assert service.status() == service.Status(True, None, port, None)


def test_the_commands(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    port = free_port()
    assert cli.main(["service"]) == 1
    assert "not installed" in capsys.readouterr().out
    assert cli.main(["service", "install", "--port", str(port)]) == 0
    assert str(port) in capsys.readouterr().out
    monkeypatch.setattr(service, "_running_version", lambda _port: None)
    assert cli.main(["service", "status"]) == 1  # a script can tell that it isn't working
    assert "not answering" in capsys.readouterr().out
    assert cli.main(["service", "uninstall"]) == 0
    assert "still there" in capsys.readouterr().out
    assert cli.main(["service", "uninstall"]) == 0
    assert "wasn't installed" in capsys.readouterr().out


def test_no_test_can_reach_the_real_launchctl() -> None:
    assert service.LAUNCHCTL == "/nonexistent/launchctl"
    with pytest.raises((OSError, MiaError)):
        service.install(free_port())


def test_mia_service_refuses_anywhere_but_a_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(MiaError, match="macOS only"):
        service.install(free_port())
    assert not service.plist_file().exists()


# Stopping, starting, and restarting.


def test_stopping_keeps_the_file_so_it_comes_back_at_login(launchctl: Launchctl) -> None:
    service.install(free_port())
    launchctl.calls.clear()
    service.stop()
    assert launchctl.verbs() == ["bootout"]
    assert service.plist_file().exists()


def test_starting_something_already_loaded_changes_nothing(launchctl: Launchctl) -> None:
    service.install(free_port())
    launchctl.calls.clear()
    launchctl.answers["print"] = [Launchctl.result(0, "pid = 4242")]
    service.start()
    assert launchctl.verbs() == ["print"]


def test_starting_something_stopped_loads_it(launchctl: Launchctl) -> None:
    service.install(free_port())
    launchctl.calls.clear()
    launchctl.answers["print"] = [Launchctl.result(113, err="Could not find service")]
    service.start()
    assert launchctl.verbs() == ["print", "bootstrap"]


def test_restarting_keeps_the_file_and_runs_it_again(launchctl: Launchctl) -> None:
    port = free_port()
    service.install(port)
    before = service.plist_file().read_bytes()
    launchctl.calls.clear()
    service.restart()
    assert launchctl.verbs() == ["bootout", "bootstrap"]
    assert service.plist_file().read_bytes() == before


@pytest.mark.parametrize("action", ["stop", "start", "restart"])
def test_those_say_so_when_it_isnt_installed(launchctl: Launchctl, capsys: pytest.CaptureFixture[str], action: str) -> None:
    assert cli.main(["service", action]) == 1
    assert "mia service install" in capsys.readouterr().err
    assert launchctl.calls == []


def test_those_commands_say_what_happens_to_a_job(launchctl: Launchctl, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["service", "install", "--port", str(free_port())]) == 0
    capsys.readouterr()
    for action in ("restart", "stop"):
        assert cli.main(["service", action]) == 0
        assert "job" in capsys.readouterr().out


def test_status_says_when_older_code_is_running(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service.install(free_port())
    monkeypatch.setattr(service, "_running_version", lambda _port: "0.0.1")
    assert cli.main(["service"]) == 0
    assert "mia service restart" in capsys.readouterr().out


def test_a_running_version_comes_from_a_real_answer() -> None:
    import http.server
    import threading

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"status": "ok", "version": "1.2.3"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_arguments: Any) -> None:
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health) as answering:
        threading.Thread(target=answering.serve_forever, daemon=True).start()
        try:
            assert service._running_version(answering.server_address[1]) == "1.2.3"
        finally:
            answering.shutdown()
    assert service._running_version(free_port()) is None


# Keeping the log small.


@contextmanager
def stderr_into_the_log() -> Iterator[Path]:
    """Points stderr at the log, appending, as launchd does. Within the test: pytest swaps stderr between its phases."""
    log = service.log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    kept = os.dup(2)
    os.dup2(held, 2)
    try:
        yield log
    finally:
        os.dup2(kept, 2)
        os.close(kept)
        os.close(held)


def test_a_big_log_moves_aside_and_writing_carries_on_from_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MAX_LOG_BYTES", 100)
    with stderr_into_the_log() as log:
        os.write(2, b"x" * 101)
        service.rotate_log()
        assert log.with_name("serve.log.1").read_bytes() == b"x" * 101
        assert log.stat().st_size == 0
        os.write(2, b"next")
        assert log.read_bytes() == b"next"  # no gap from the old length, which would leave it as large as before
        assert log.with_name("serve.log.1").stat().st_mode & 0o077 == 0
        assert not log.with_name("serve.log.partial").exists()


def test_only_one_old_log_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MAX_LOG_BYTES", 100)
    with stderr_into_the_log() as log:
        for fill in (b"a", b"b", b"c"):
            os.write(2, fill * 101)
            service.rotate_log()
        assert log.with_name("serve.log.1").read_bytes() == b"c" * 101
        assert sorted(path.name for path in log.parent.iterdir()) == ["serve.log", "serve.log.1"]


def test_a_small_log_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MAX_LOG_BYTES", 100)
    with stderr_into_the_log() as log:
        os.write(2, b"x" * 100)
        service.rotate_log()
        assert log.stat().st_size == 100
        assert not log.with_name("serve.log.1").exists()


def test_a_log_this_process_isnt_writing_to_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mia serve started by hand, beside the log of a service that isn't running.
    monkeypatch.setattr(service, "MAX_LOG_BYTES", 100)
    log = service.log_file()
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * 101)
    service.rotate_log()
    assert log.stat().st_size == 101


def test_no_log_or_no_stderr_is_no_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    service.rotate_log()
    log = service.log_file()
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * 101)
    monkeypatch.setattr(service, "MAX_LOG_BYTES", 100)

    def closed(_descriptor: int) -> os.stat_result:
        raise OSError("closed")

    monkeypatch.setattr(os, "fstat", closed)
    service.rotate_log()
    assert log.stat().st_size == 101


def test_the_supervisor_checks_the_log_now_and_then(monkeypatch: pytest.MonkeyPatch) -> None:
    from mia import supervisor as supervising

    clock = [1000.0]
    checks: list[float] = []
    monkeypatch.setattr("mia.supervisor.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "rotate_log", lambda: checks.append(clock[0]))
    watcher = supervising.Supervisor()

    class Alive:
        def poll(self) -> None:
            return None

    watcher._process = Alive()  # type: ignore[assignment]
    watcher._check()
    clock[0] += 1
    watcher._check()
    assert len(checks) == 1
    clock[0] += supervising.LOG_CHECK_SECONDS
    watcher._check()
    assert len(checks) == 2


def test_a_server_that_cant_start_still_keeps_its_log_small(monkeypatch: pytest.MonkeyPatch) -> None:
    # launchd restarts it every 10 seconds, so this could go on for months, and it never reaches the supervisor.
    from mia import server

    checks: list[int] = []
    monkeypatch.setattr(service, "rotate_log", lambda: checks.append(1))
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(MiaError, match="in use"):
            server.serve(int(taken.getsockname()[1]))
    assert checks == [1]


# Handing back memory.


def _idle_worker(monkeypatch: pytest.MonkeyPatch, seconds: str = "1000") -> Worker:
    monkeypatch.setenv("MIA_IDLE_SECONDS", seconds)
    return Worker(lambda _model: FakeEngine())


def test_a_worker_with_a_model_loaded_goes_idle_after_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("mia.worker.time.monotonic", lambda: clock[0])
    running = _idle_worker(monkeypatch)
    jobs.submit(Request("a fox", 1, 512, 512, 1), catalog.DEFAULT.id)
    assert running.step() is True
    clock[0] += 1000
    assert not running.idle_too_long()  # at the limit, not past it
    clock[0] += 1
    assert running.idle_too_long()


def test_each_job_starts_the_wait_again(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("mia.worker.time.monotonic", lambda: clock[0])
    running = _idle_worker(monkeypatch)
    for _ in range(2):
        jobs.submit(Request("a fox", 1, 512, 512, 1), catalog.DEFAULT.id)
        running.step()
        clock[0] += 999
        assert not running.idle_too_long()


def test_a_worker_with_nothing_loaded_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("mia.worker.time.monotonic", lambda: clock[0])
    running = _idle_worker(monkeypatch)
    clock[0] += 10**6
    assert not running.idle_too_long()


def test_a_failed_job_counts_as_activity_and_a_failed_load_stays_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("mia.worker.time.monotonic", lambda: clock[0])
    monkeypatch.setenv("MIA_IDLE_SECONDS", "1000")

    def no_model(_model: catalog.Model) -> FakeEngine:
        raise MiaError("it isn't downloaded")

    running = Worker(no_model)
    jobs.submit(Request("a fox", 1, 512, 512, 1), catalog.DEFAULT.id)
    running.step()
    clock[0] += 10**6
    assert not running.idle_too_long()


@pytest.mark.parametrize(("value", "expected"), [("120", 120.0), ("0.5", 0.5), ("0", 600.0), ("-1", 600.0), ("soon", 600.0), ("", 600.0)])
def test_the_idle_setting(monkeypatch: pytest.MonkeyPatch, value: str, expected: float) -> None:
    from mia import worker as working

    monkeypatch.setenv("MIA_IDLE_SECONDS", value)
    assert working.idle_seconds() == expected


def test_the_default_wait_is_ten_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    from mia import worker as working

    monkeypatch.delenv("MIA_IDLE_SECONDS", raising=False)
    assert working.idle_seconds() == 600


def test_the_agent_gets_the_idle_setting_as_it_was_given(launchctl: Launchctl, monkeypatch: pytest.MonkeyPatch) -> None:
    # It's a number, and treating it as a path turned it into a folder, which the worker then ignored.
    monkeypatch.setenv("MIA_IDLE_SECONDS", "120")
    service.install(free_port())
    assert written()["EnvironmentVariables"]["MIA_IDLE_SECONDS"] == "120"


def test_a_refusal_for_the_gpu_says_how_to_stop_the_service() -> None:
    from mia import gpu

    with gpu.hold(), pytest.raises(gpu.Busy, match="mia service stop"), gpu.hold():
        pass


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
