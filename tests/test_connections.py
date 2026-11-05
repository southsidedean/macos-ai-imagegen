"""What someone can hold open, over real sockets, because these limits live below anything a test client reaches."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from mia import jobs, server, tokens
from mia.engine import Request

ARRIVAL = 0.4


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "mia"))
    monkeypatch.setenv("MIA_ENGINE", "fake")
    monkeypatch.setattr(server, "ARRIVAL_SECONDS", ARRIVAL)


@pytest.fixture
def port() -> Iterator[int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    running = uvicorn.Server(server._config(server.create_app()))
    thread = threading.Thread(target=running.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not running.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert running.started
    try:
        yield int(listener.getsockname()[1])
    finally:
        running.should_exit = True
        thread.join(10)
        listener.close()


@contextmanager
def connected(port: int) -> Iterator[socket.socket]:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as held:
        yield held


def ended_by_server(held: socket.socket) -> bool:
    """Waits for the server to hang up. A timeout means it didn't."""
    try:
        return held.recv(4096) == b""
    except ConnectionResetError:
        return True
    except TimeoutError:
        return False


def request_bytes(path: str = "/v1/health", extra: str = "") -> bytes:
    return f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{extra}\r\n".encode()


def status_of_answer(held: socket.socket) -> int:
    """Reads one whole answer, as a following request can only be sent after it. Its parts can arrive separately."""
    seen = b""
    while b"\r\n\r\n" not in seen:
        seen += held.recv(4096)
    head, _, body = seen.partition(b"\r\n\r\n")
    length = re.search(rb"content-length: (\d+)", head, re.IGNORECASE)
    assert length is not None
    while len(body) < int(length[1]):
        body += held.recv(4096)
    return int(head.split()[1])


def answers(port: int) -> bool:
    with connected(port) as held:
        held.sendall(request_bytes())
        return status_of_answer(held) == 200


def test_a_connection_that_never_sends_anything_is_dropped(port: int) -> None:
    with connected(port) as held:
        began = time.monotonic()
        assert ended_by_server(held)
        assert time.monotonic() - began >= ARRIVAL * 0.8


def test_a_request_sent_a_byte_at_a_time_cant_stay_open(port: int) -> None:
    with connected(port) as held:
        began = time.monotonic()
        with pytest.raises(OSError):  # broken pipe or reset, whichever comes first
            for byte in request_bytes(extra="X-Padding: " + "x" * 200 + "\r\n"):
                held.sendall(bytes([byte]))
                time.sleep(0.05)
            raise AssertionError("the whole request went through")
        assert time.monotonic() - began < 5


def test_a_body_that_never_comes_is_dropped_even_with_a_token(port: int) -> None:
    issued = tokens.create("real")
    with connected(port) as held:
        held.sendall(
            b"POST /v1/jobs HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: 100\r\n"
            + f"Authorization: Bearer {issued}\r\n\r\n".encode()
            + b"{"
        )
        assert ended_by_server(held)
    assert answers(port)
    assert jobs.counts()[jobs.QUEUED] == 0


def test_no_token_is_refused_without_waiting_for_the_body(port: int) -> None:
    with connected(port) as held:
        held.sendall(b"POST /v1/jobs HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 100\r\n\r\n")
        assert status_of_answer(held) == 401


def test_an_ordinary_connection_can_go_between_requests_for_longer_than_the_limit(port: int) -> None:
    with connected(port) as held:
        held.sendall(request_bytes())
        assert status_of_answer(held) == 200
        time.sleep(ARRIVAL * 1.5)
        held.sendall(request_bytes())
        assert status_of_answer(held) == 200


def test_a_half_sent_second_request_is_dropped_too(port: int) -> None:
    # Any data cancels uvicorn's timer between requests, so this would otherwise stay.
    with connected(port) as held:
        held.sendall(request_bytes())
        assert status_of_answer(held) == 200
        held.sendall(b"GET /v1/heal")
        assert ended_by_server(held)


def test_an_event_stream_outlives_the_limit_on_arriving(port: int) -> None:
    issued = tokens.create("real")
    job, _ = jobs.submit(Request("a fox", 1, 512, 512, 2), "flux2-klein-4b-q4")
    with connected(port) as held:
        held.sendall(request_bytes(f"/v1/jobs/{job.id}/events", f"Authorization: Bearer {issued}\r\n"))
        seen = b""
        while b"event: progress" not in seen:
            seen += held.recv(4096)
        time.sleep(ARRIVAL * 2)
        jobs.cancel(job.id)
        while b"event: done" not in seen:
            more = held.recv(4096)
            assert more, "the stream ended early"
            seen += more
    assert json.loads(seen.split(b"data: ")[-1].split(b"\n")[0])["state"] == "cancelled"


def pipeline_and_never_read(port: int) -> socket.socket:
    """Answers pile up in the buffers, until the transport has to stop writing. No token is needed for this."""
    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    held.connect(("127.0.0.1", port))
    held.settimeout(10)
    held.sendall(b"GET /openapi.json HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" * 3000)
    return held


def test_requests_that_are_never_read_dont_hold_a_connection_for_good(port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_STALL_SECONDS", 0.5)
    monkeypatch.setattr(server, "MAX_CONNECTIONS_PER_ADDRESS", 3)
    held = [pipeline_and_never_read(port) for _ in range(3)]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            if answers(port):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise AssertionError("the unread connections kept their slots")
    for each in held:
        each.close()


def test_a_slow_reader_who_keeps_reading_gets_the_whole_answer(port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_STALL_SECONDS", 0.5)
    monkeypatch.setattr(server, "SEND_BUFFER_BYTES", 32 * 1024)  # as small as the limit is short, as a pause lasts until it drains
    count = 300
    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    held.connect(("127.0.0.1", port))
    held.settimeout(10)
    held.sendall(b"GET /openapi.json HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" * count)
    seen = bytearray()
    began = time.monotonic()
    with held:
        while piece := held.recv(65536):  # until the server hangs up, once it has sent everything
            seen += piece
            # Slow enough to take longer than the limit in all, with no one gap as long as it.
            time.sleep(0.02 if time.monotonic() - began < server.WRITE_STALL_SECONDS * 3 else 0)
    assert seen.count(b"HTTP/1.1 200 OK") == count
    assert time.monotonic() - began > server.WRITE_STALL_SECONDS * 2, "too quick to prove anything"


@pytest.mark.parametrize("limit", ["MAX_CONNECTIONS", "MAX_CONNECTIONS_PER_ADDRESS"])
def test_connections_are_capped_and_a_slot_comes_back(port: int, monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    monkeypatch.setattr(server, "ARRIVAL_SECONDS", 30.0)
    monkeypatch.setattr(server, limit, 2)
    with connected(port) as first, connected(port) as second:
        first.sendall(request_bytes())  # proves each is in before the next arrives
        assert status_of_answer(first) == 200
        second.sendall(request_bytes())
        assert status_of_answer(second) == 200
        with connected(port) as third:
            assert ended_by_server(third)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if answers(port):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("no slot came back after the others closed")
