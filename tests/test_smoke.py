"""The real server and worker, as separate processes, with the fake engine. Nothing is mocked but the model.

This is what 0.3 promises: a worker killed mid-job leaves the API up, the failure recorded, and the
next job working.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.timeout(60)

STEP_SECONDS = "0.2"
LONG = {"prompt": "a long job", "width": 512, "height": 512, "steps": 50, "seed": 1}
SHORT = {"prompt": "a short job", "width": 512, "height": 512, "steps": 2, "seed": 2}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for[T](check: Callable[[], T | None], seconds: float = 20) -> T:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError(f"waited {seconds} seconds for {check}")


class Service:
    def __init__(self, folder: Path, port: int | None = None) -> None:
        self.folder = folder
        self.port = port or free_port()
        self.env = {**os.environ, "MIA_HOME": str(folder), "MIA_ENGINE": "fake", "MIA_FAKE_STEP_SECONDS": STEP_SECONDS}
        self.token = self.mia("token", "create", "--for", f"smoke-{time.monotonic_ns()}").stdout.strip()
        # A session of its own, so a test can send Control-C to the whole group, the way a terminal does.
        self.process = subprocess.Popen(
            [sys.executable, "-m", "mia", "serve", "--port", str(self.port)], env=self.env, start_new_session=True
        )
        try:
            wait_for(self.healthy)
        except BaseException:
            self.stop()  # the fixture never gets to, and this would outlive the test
            raise

    def mia(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "mia", *arguments], env=self.env, capture_output=True, text=True, check=True)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            payload = json.dumps(body) if body is not None else None
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            connection.request(method, path, payload, headers)
            answer = connection.getresponse()
            return answer.status, answer.read()
        finally:
            connection.close()

    def healthy(self) -> bool:
        try:
            return self.request("GET", "/v1/health")[0] == 200
        except OSError:
            return False

    def submit(self, body: dict[str, Any]) -> str:
        status, data = self.request("POST", "/v1/jobs", body)
        assert status == 202, data
        return str(json.loads(data)["id"])

    def job(self, job_id: str) -> dict[str, Any]:
        status, data = self.request("GET", f"/v1/jobs/{job_id}")
        assert status == 200
        return dict(json.loads(data))

    def finished(self, job_id: str) -> dict[str, Any]:
        def done() -> dict[str, Any] | None:
            job = self.job(job_id)
            return job if job["state"] in ("done", "failed", "cancelled") else None

        return wait_for(done)

    def running(self, job_id: str) -> None:
        wait_for(lambda: self.job(job_id)["step"] >= 1)

    def worker_pid(self) -> int:
        return int((self.folder / "gpu.lock").read_text())

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(30)


@pytest.fixture
def service(tmp_path: Path) -> Iterator[Service]:
    running = Service(tmp_path / "mia")
    try:
        yield running
    finally:
        running.stop()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_job_runs_from_start_to_a_downloaded_image(service: Service) -> None:
    job = service.finished(service.submit(SHORT))
    assert job["state"] == "done"
    status, picture = service.request("GET", job["links"]["file"])
    assert status == 200 and picture.startswith(b"\x89PNG\r\n\x1a\n")
    status, data = service.request("GET", "/v1/status")
    assert status == 200 and json.loads(data)["worker"]["running"] is True


def test_killing_the_worker_mid_job_leaves_the_api_up_and_the_failure_recorded(service: Service) -> None:
    job_id = service.submit(LONG)
    service.running(job_id)
    victim = service.worker_pid()
    os.kill(victim, signal.SIGKILL)  # what running out of memory looks like

    failed = service.finished(job_id)
    assert failed["state"] == "failed"
    assert "worker stopped" in failed["error"]
    assert service.healthy()
    # Not retried, and the next job gets a new worker.
    after = service.finished(service.submit(SHORT))
    assert after["state"] == "done"
    assert service.worker_pid() != victim
    assert service.job(job_id)["state"] == "failed"


def test_jobs_queued_behind_a_dead_worker_still_run(service: Service) -> None:
    first = service.submit(LONG)
    waiting = service.submit(SHORT)
    service.running(first)
    os.kill(service.worker_pid(), signal.SIGKILL)
    assert service.finished(first)["state"] == "failed"
    assert service.finished(waiting)["state"] == "done"


def test_cancelling_a_running_job_stops_it_soon_and_the_worker_carries_on(service: Service) -> None:
    job_id = service.submit(LONG)
    service.running(job_id)
    worker = service.worker_pid()
    asked = time.monotonic()
    status, _ = service.request("DELETE", f"/v1/jobs/{job_id}")
    assert status == 202
    assert service.finished(job_id)["state"] == "cancelled"
    assert time.monotonic() - asked < 5
    assert service.job(job_id)["image"] is None
    assert service.finished(service.submit(SHORT))["state"] == "done"
    assert service.worker_pid() == worker  # the same worker, with its model still loaded


def test_changing_model_starts_a_new_worker(service: Service) -> None:
    assert service.finished(service.submit(SHORT))["state"] == "done"
    worker = service.worker_pid()
    other = service.finished(service.submit({**SHORT, "model": "flux1-schnell-q4"}))
    assert other["state"] == "done"
    assert service.worker_pid() != worker


def test_stopping_the_server_stops_the_worker_and_records_the_job(service: Service) -> None:
    job_id = service.submit(LONG)
    service.running(job_id)
    worker = service.worker_pid()
    service.stop()
    # uvicorn shuts down cleanly, then raises the signal again, so the exit says what stopped it.
    assert service.process.returncode == -signal.SIGTERM
    assert not alive(worker)  # already gone: the shutdown waited for it
    code = "import sys; from mia import jobs; job = jobs.get(sys.argv[1]); print(job.state, job.error)"
    answer = subprocess.run([sys.executable, "-c", code, job_id], env=service.env, capture_output=True, text=True, check=True)
    assert answer.stdout.startswith("failed") and "stopped" in answer.stdout


def test_a_worker_whose_server_was_killed_doesnt_linger(service: Service) -> None:
    assert service.finished(service.submit(SHORT))["state"] == "done"
    worker = service.worker_pid()
    service.process.kill()
    service.process.wait()
    wait_for(lambda: not alive(worker))


def raw_request(port: int, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(data)
        return connection.recv(4096)


def test_a_chunked_body_with_no_token_cant_get_past_the_size_limit(service: Service) -> None:
    # Against the real HTTP parser, where chunked wins over Content-Length. This reached 482 MB of memory.
    for headers in (b"Transfer-Encoding: chunked\r\n", b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n"):
        answer = raw_request(service.port, b"POST /v1/jobs HTTP/1.1\r\nHost: 127.0.0.1\r\n" + headers + b"\r\n")
        assert answer.startswith(b"HTTP/1.1 411"), answer


def test_a_body_larger_than_it_said_isnt_read_as_one(service: Service) -> None:
    answer = raw_request(service.port, b"POST /v1/jobs HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 99999999\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 413"), answer


def test_a_second_server_on_the_same_port_leaves_the_first_alone(service: Service) -> None:
    job_id = service.submit(LONG)
    service.running(job_id)
    second = subprocess.run(
        [sys.executable, "-m", "mia", "serve", "--port", str(service.port)], env=service.env, capture_output=True, text=True, check=False
    )
    assert second.returncode == 1 and "already in use" in second.stderr
    assert service.job(job_id)["state"] == "running"  # this used to fail a job that was still running
    assert service.request("DELETE", f"/v1/jobs/{job_id}")[0] == 202
    assert service.finished(job_id)["state"] == "cancelled"


@pytest.mark.parametrize("how", ["term", "control-c"])
def test_stopping_mid_job_ends_that_job_and_leaves_the_next_queued(service: Service, how: str) -> None:
    # The fake engine swallows interrupts the way mflux's step loop does, so this covers what really happens.
    running = service.submit(LONG)
    waiting = service.submit(SHORT)
    service.running(running)
    worker = service.worker_pid()
    asked = time.monotonic()
    if how == "term":
        service.process.terminate()
    else:
        os.killpg(service.process.pid, signal.SIGINT)
    service.process.wait(30)
    assert time.monotonic() - asked < 15  # it doesn't sit out the time limit and get killed
    assert not alive(worker)
    code = "import sys; from mia import jobs; print(*(jobs.get(i).state for i in sys.argv[1:]))"
    states = subprocess.run([sys.executable, "-c", code, running, waiting], env=service.env, capture_output=True, text=True, check=True)
    assert states.stdout.split() == ["failed", "queued"]  # the queue survives a restart
    restarted = Service(service.folder)
    try:
        assert restarted.finished(waiting)["state"] == "done"
    finally:
        restarted.stop()


def test_an_open_event_stream_doesnt_hold_up_stopping(service: Service) -> None:
    job_id = service.submit(LONG)
    stream = http.client.HTTPConnection("127.0.0.1", service.port, timeout=30)
    stream.request("GET", f"/v1/jobs/{job_id}/events", headers={"Authorization": f"Bearer {service.token}"})
    assert stream.getresponse().status == 200
    asked = time.monotonic()
    service.process.terminate()
    service.process.wait(30)
    assert time.monotonic() - asked < 15
    stream.close()


def test_revoking_a_token_ends_its_open_stream(tmp_path: Path) -> None:
    service = Service(tmp_path / "mia")
    try:
        name = "stream-device"
        token = service.mia("token", "create", "--for", name).stdout.strip()
        job_id = service.submit(LONG)
        stream = http.client.HTTPConnection("127.0.0.1", service.port, timeout=30)
        stream.request("GET", f"/v1/jobs/{job_id}/events", headers={"Authorization": f"Bearer {token}"})
        answer = stream.getresponse()
        assert answer.status == 200
        service.mia("token", "revoke", name)
        asked = time.monotonic()
        answer.read()  # returns only when the server ends the stream
        assert time.monotonic() - asked < 20
        stream.close()
        service.request("DELETE", f"/v1/jobs/{job_id}")
    finally:
        service.stop()


def test_too_many_open_streams_are_refused_and_the_api_stays_quick(service: Service) -> None:
    job_id = service.submit(LONG)
    opened = []
    try:
        for _ in range(8):
            stream = http.client.HTTPConnection("127.0.0.1", service.port, timeout=10)
            stream.request("GET", f"/v1/jobs/{job_id}/events", headers={"Authorization": f"Bearer {service.token}"})
            assert stream.getresponse().status == 200
            opened.append(stream)
        assert service.request("GET", f"/v1/jobs/{job_id}/events")[0] == 429
        asked = time.monotonic()
        assert service.healthy()
        assert time.monotonic() - asked < 2
    finally:
        for stream in opened:
            stream.close()
    assert service.request("DELETE", f"/v1/jobs/{job_id}")[0] == 202
    # Closing them gives the slots back, once each stream's next write notices.
    wait_for(lambda: service.finished(job_id) and True)


def test_killing_the_worker_at_each_gap_leaves_a_consistent_answer(service: Service) -> None:
    # Whatever moment it dies at, a finished image is linked to its job, or the job failed and no image is left.
    for delay in (0.25, 0.5, 0.75):
        job_id = service.submit({**SHORT, "steps": 3})
        time.sleep(delay)
        pid = service.worker_pid()
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
        job = service.finished(job_id)
        if job["state"] == "done":
            assert service.request("GET", job["links"]["file"])[0] == 200
        else:
            assert job["state"] == "failed" and job["image"] is None
        wait_for(lambda: service.finished(service.submit({**SHORT, "steps": 1}))["state"] == "done")
    images = service.folder / "images"
    status, data = service.request("GET", "/v1/images?limit=200")
    assert status == 200
    recorded = {image["id"] for image in json.loads(data)["images"]}
    # Swept when a worker starts: only recorded pictures remain.
    assert {path.stem for path in images.glob("*.png")} <= recorded or wait_for(
        lambda: {path.stem for path in images.glob("*.png")} <= recorded
    )


def test_nothing_private_reaches_the_servers_output(tmp_path: Path) -> None:
    home = tmp_path / "mia"
    env = {**os.environ, "MIA_HOME": str(home), "MIA_ENGINE": "fake"}
    token = subprocess.run([sys.executable, "-m", "mia", "token", "create", "--for", "x"], env=env, capture_output=True, text=True, check=True).stdout.strip()
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "mia", "serve", "--port", str(port)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:

        def call(method: str, path: str, body: str | None = None) -> int:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request(method, path, body, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            result = connection.getresponse()
            result.read()
            connection.close()
            return result.status

        def up() -> bool:
            try:
                return call("GET", "/v1/health") == 200
            except OSError:
                return False

        wait_for(up)
        assert call("POST", "/v1/jobs", json.dumps({"prompt": "a secret-looking prompt", "width": 512, "height": 512, "steps": 1})) == 202
        assert call("GET", "/v1/images?search=secret-looking") == 200
        assert call("POST", "/v1/jobs", '{"prompt": "a secret-looking \\ud83d prompt"}') == 422
        time.sleep(1)
    finally:
        process.terminate()
        output = process.communicate(timeout=30)[0]
    assert "secret-looking" not in output
    assert token not in output
