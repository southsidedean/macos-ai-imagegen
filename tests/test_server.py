"""The REST API, in process, with the fake engine. test_smoke.py runs it for real."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mia import catalog, history, jobs, paths, server, tokens
from mia.fake_engine import FakeEngine
from mia.generate import generate
from mia.engine import Request
from mia.worker import Worker

BASE = "http://127.0.0.1:8765"
PNG = b"\x89PNG\r\n\x1a\n"
BODY = {"prompt": "a red fox", "width": 512, "height": 512, "steps": 2, "seed": 42}


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "mia"))
    monkeypatch.setenv("MIA_ENGINE", "fake")


@pytest.fixture
def api() -> Iterator[TestClient]:
    with TestClient(server.create_app(), base_url=BASE) as client:
        yield client


def bearer(scope: str = tokens.ADMIN, name: str = "test") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.create(name, scope)}"}


def run_worker() -> None:
    Worker(lambda _model: FakeEngine()).step()


def test_health_needs_no_token_and_says_nothing_private(api: TestClient) -> None:
    answer = api.get("/v1/health")
    assert answer.status_code == 200
    assert set(answer.json()) == {"status", "version"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/status"),
        ("GET", "/v1/jobs"),
        ("POST", "/v1/jobs"),
        ("GET", "/v1/jobs/abc"),
        ("GET", "/v1/jobs/abc/events"),
        ("DELETE", "/v1/jobs/abc"),
        ("GET", "/v1/images"),
        ("GET", "/v1/images/abc"),
        ("GET", "/v1/images/abc/file"),
        ("DELETE", "/v1/images/abc"),
        ("GET", "/v1/models"),
    ],
)
def test_every_other_route_needs_a_token(api: TestClient, method: str, path: str) -> None:
    answer = api.request(method, path, json=BODY if method == "POST" else None)
    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == "Bearer"


def _needs_a_token(dependant: Any) -> bool:
    return any(d.call.__name__ == "check" or _needs_a_token(d) for d in dependant.dependencies)


def test_every_route_in_the_app_needs_a_token_unless_named_here() -> None:
    # Walks the app itself, so a new route can't ship without a token by being left off a list.
    public = {("GET", "/v1/health"), ("GET", "/openapi.json"), ("HEAD", "/openapi.json")}
    checked = 0
    for route in server.create_app().routes:
        # A mount or a websocket route has no methods, and MCP is likely to be one: name it here, or fail.
        assert getattr(route, "methods", None), f"{route} isn't a route this test understands"
        for method in getattr(route, "methods", None) or ():
            path = getattr(route, "path", "")
            if (method, path) in public:
                continue
            assert isinstance(route, APIRoute), f"{method} {path} isn't an API route, so nothing checks a token"
            assert _needs_a_token(route.dependant), f"{method} {path} doesn't check a token"
            checked += 1
    assert checked >= 11  # the walk really did find the routes


@pytest.mark.parametrize("header", ["Bearer mia_wrong", "Basic abc", "mia_abc", "Bearer ", ""])
def test_bad_credentials_are_refused(api: TestClient, header: str) -> None:
    tokens.create("real")
    assert api.get("/v1/jobs", headers={"Authorization": header}).status_code == 401


def test_a_token_in_the_url_doesnt_count(api: TestClient) -> None:
    issued = tokens.create("real")
    assert api.get("/v1/jobs", params={"token": issued, "authorization": f"Bearer {issued}"}).status_code == 401


def test_a_revoked_token_stops_working(api: TestClient) -> None:
    headers = bearer(tokens.READ, "going")
    assert api.get("/v1/jobs", headers=headers).status_code == 200
    tokens.revoke("going")
    assert api.get("/v1/jobs", headers=headers).status_code == 401


def test_scopes_are_enforced(api: TestClient) -> None:
    reader, maker, owner = bearer(tokens.READ, "r"), bearer(tokens.GENERATE, "g"), bearer(tokens.ADMIN, "a")
    assert api.get("/v1/jobs", headers=reader).status_code == 200
    assert api.post("/v1/jobs", json=BODY, headers=reader).status_code == 403
    job = api.post("/v1/jobs", json=BODY, headers=maker)
    assert job.status_code == 202
    assert api.delete(f"/v1/jobs/{job.json()['id']}", headers=reader).status_code == 403
    assert api.delete(f"/v1/jobs/{job.json()['id']}", headers=maker).status_code == 202
    record = generate(Request("x", 1, 512, 512, 1), catalog.DEFAULT, FakeEngine())
    assert api.delete(f"/v1/images/{record.id}", headers=maker).status_code == 403
    assert api.delete(f"/v1/images/{record.id}", headers=owner).status_code == 204


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8765",
        "127.0.0.1.evil.example",
        "",
        "localhost.evil.example",
        "localhost:evil.example",
        "localhost:x@example.com",
        "[::1]evil.example",
        "[::1",
        "http://localhost/evil",
        "127.0.0.1:1, evil.example",
    ],
)
def test_another_host_is_refused_even_with_a_token(api: TestClient, host: str) -> None:
    # A web page that points its own name at this Mac arrives with that name as the Host: DNS rebinding.
    assert api.get("/v1/jobs", headers={**bearer(), "Host": host}).status_code == 403
    assert api.get("/v1/health", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_this_machines_names_are_allowed(api: TestClient, host: str) -> None:
    assert api.get("/v1/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "null", "http://127.0.0.1.evil.example", "http://localhost:x@example.com", "ftp://localhost"]
)
def test_another_origin_is_refused(api: TestClient, origin: str) -> None:
    answer = api.get("/v1/jobs", headers={**bearer(), "Origin": origin})
    assert answer.status_code == 403


def test_this_machines_origin_is_allowed(api: TestClient) -> None:
    assert api.get("/v1/health", headers={"Origin": "http://localhost:8765"}).status_code == 200


def test_a_repeated_host_or_origin_is_refused() -> None:
    assert server.Guard._refuse("GET", [("host", "localhost"), ("host", "evil.example")]) is not None
    assert server.Guard._refuse("GET", [("host", "localhost"), ("origin", "http://localhost"), ("origin", "http://localhost")]) is not None
    assert server.Guard._refuse("GET", [("host", "localhost")]) is None


def test_answers_arent_cached(api: TestClient) -> None:
    assert api.get("/v1/jobs", headers=bearer()).headers["cache-control"] == "no-store"


def test_a_large_body_or_one_with_no_length_is_refused(api: TestClient) -> None:
    headers = {**bearer(), "Content-Type": "application/json"}
    assert api.post("/v1/jobs", content=b"x" * (server.MAX_BODY + 1), headers=headers).status_code == 413

    def chunks() -> Iterator[bytes]:
        yield b"{}"

    assert api.post("/v1/jobs", content=chunks(), headers=headers).status_code == 411


def test_a_job_goes_from_submitted_to_a_downloaded_image(api: TestClient) -> None:
    headers = bearer(tokens.GENERATE)
    created = api.post("/v1/jobs", json=BODY, headers=headers)
    assert created.status_code == 202
    job = created.json()
    assert job["state"] == "queued" and created.headers["location"] == f"/v1/jobs/{job['id']}"
    assert "token" not in job and "idempotency_key" not in job
    run_worker()
    finished = api.get(job["links"]["self"], headers=headers).json()
    assert finished["state"] == "done" and finished["step"] == 2
    image = api.get(finished["links"]["image"], headers=headers).json()
    assert image["prompt"] == "a red fox" and image["seed"] == 42
    assert "file" not in image  # a path on the Mac
    picture = api.get(finished["links"]["file"], headers=headers)
    assert picture.status_code == 200 and picture.content.startswith(PNG)
    assert picture.headers["content-type"] == "image/png"
    assert picture.headers["cache-control"] == "private, no-store"
    assert picture.headers["x-content-type-options"] == "nosniff"


def test_a_seed_is_chosen_when_none_is_given_and_steps_come_from_the_model(api: TestClient) -> None:
    job = api.post("/v1/jobs", json={"prompt": "a fox"}, headers=bearer()).json()
    assert 0 <= job["seed"] < 2**32
    assert job["steps"] == catalog.DEFAULT.steps and job["model"] == catalog.DEFAULT.id
    assert (job["width"], job["height"]) == (1024, 1024)


@pytest.mark.parametrize(
    "bad",
    [
        {"prompt": "  "},
        {"prompt": "x" * 4001},
        {"prompt": "x", "width": 1000},
        {"prompt": "x", "steps": 500},
        {"prompt": "x", "steps": 0},
        {"prompt": "x", "seed": -1},
        {"prompt": "x", "model": "nope"},
        {"prompt": "x", "unknown": 1},
        {},
    ],
)
def test_bad_requests_are_refused_and_nothing_is_queued(api: TestClient, bad: dict[str, object]) -> None:
    assert api.post("/v1/jobs", json=bad, headers=bearer()).status_code == 422
    assert jobs.counts()[jobs.QUEUED] == 0


def test_a_refusal_doesnt_echo_what_was_sent(api: TestClient) -> None:
    answer = api.post("/v1/jobs", json={"prompt": "x" * 4001, "width": "very private"}, headers=bearer())
    assert answer.status_code == 422
    assert "very private" not in answer.text and "xxxx" not in answer.text
    assert {"loc", "msg", "type"} == set(answer.json()["detail"][0])


def test_half_an_emoji_gets_a_refusal_not_a_crash(api: TestClient) -> None:
    # It can't be encoded, so echoing it failed the answer itself, and the server logged the whole prompt.
    body = b'{"prompt": "a private \\ud83d prompt"}'
    answer = api.post("/v1/jobs", content=body, headers={**bearer(), "Content-Type": "application/json"})
    assert answer.status_code == 422
    assert "private" not in answer.text


def test_nearly_out_of_disk_is_refused_at_once(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = bearer()
    assert api.post("/v1/jobs", json=BODY, headers=headers).status_code == 202
    monkeypatch.setattr(server, "MIN_FREE_BYTES", 2**62)
    assert api.post("/v1/jobs", json=BODY, headers=headers).status_code == 507
    assert jobs.counts()[jobs.QUEUED] == 1


def test_a_model_that_isnt_downloaded_is_refused_at_once(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = bearer()
    monkeypatch.delenv("MIA_ENGINE")
    answer = api.post("/v1/jobs", json=BODY, headers=headers)
    assert answer.status_code == 409
    assert f"mia models pull {catalog.DEFAULT.id}" in answer.json()["detail"]


def test_retrying_with_a_key_doesnt_make_a_second_job(api: TestClient) -> None:
    headers = {**bearer(), "Idempotency-Key": "one"}
    first = api.post("/v1/jobs", json={"prompt": "a fox"}, headers=headers).json()
    # No seed, so a naive comparison would see a new random seed and call this a different request.
    second = api.post("/v1/jobs", json={"prompt": "a fox"}, headers=headers).json()
    assert first["id"] == second["id"]
    assert jobs.counts()[jobs.QUEUED] == 1
    assert api.post("/v1/jobs", json={"prompt": "another"}, headers=headers).status_code == 409
    assert api.post("/v1/jobs", json={"prompt": "a fox", "seed": first["seed"] + 1}, headers=headers).status_code == 409


def test_retrying_a_job_that_exists_is_never_refused(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {**bearer(), "Idempotency-Key": "one"}
    first = api.post("/v1/jobs", json=BODY, headers=headers).json()
    monkeypatch.setattr(server, "SUBMITS_PER_MINUTE", 0)  # the quota is spent
    monkeypatch.setattr(server, "MIN_FREE_BYTES", 2**62)  # the disk is full
    monkeypatch.delenv("MIA_ENGINE")  # the model isn't there
    retry = api.post("/v1/jobs", json=BODY, headers=headers)
    assert retry.status_code == 202 and retry.json()["id"] == first["id"]
    assert "planned_image" not in retry.json()


def test_a_stream_gives_back_its_slot_even_if_it_never_starts() -> None:
    import asyncio

    released: list[int] = []

    async def never() -> Any:
        yield ""

    async def main() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        async def send(_message: Any) -> None:
            raise OSError("the client is gone")

        response = server._Stream(never(), lambda: released.append(1))
        with contextlib.suppress(Exception):  # whichever way the framework reports it
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    asyncio.run(main())
    assert released == [1]


def test_a_full_queue_says_when_to_come_back(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "MAX_QUEUED", 1)
    headers = bearer()
    assert api.post("/v1/jobs", json=BODY, headers=headers).status_code == 202
    full = api.post("/v1/jobs", json=BODY, headers=headers)
    assert full.status_code == 429 and full.headers["retry-after"] == "60"


def test_requests_and_submissions_are_limited_per_device(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUBMITS_PER_MINUTE", 2)
    monkeypatch.setattr(server, "REQUESTS_PER_MINUTE", 4)
    first, other = bearer(name="first"), bearer(name="other")
    assert api.post("/v1/jobs", json=BODY, headers=first).status_code == 202
    assert api.post("/v1/jobs", json=BODY, headers=first).status_code == 202
    limited = api.post("/v1/jobs", json=BODY, headers=first)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
    assert api.get("/v1/jobs", headers=first).status_code == 200
    assert api.get("/v1/jobs", headers=first).status_code == 429
    assert api.get("/v1/jobs", headers=other).status_code == 200  # another device isn't affected


def test_cancelling_through_the_api(api: TestClient) -> None:
    headers = bearer()
    job = api.post("/v1/jobs", json=BODY, headers=headers).json()
    assert api.delete(f"/v1/jobs/{job['id']}", headers=headers).json()["state"] == "cancelled"
    assert api.delete(f"/v1/jobs/{job['id']}", headers=headers).status_code == 409
    assert api.delete("/v1/jobs/nothing", headers=headers).status_code == 404


def test_events_end_with_the_finished_job(api: TestClient) -> None:
    headers = bearer()
    job = api.post("/v1/jobs", json=BODY, headers=headers).json()
    run_worker()
    with api.stream("GET", f"/v1/jobs/{job['id']}/events", headers=headers) as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        text = "".join(stream.iter_text())
    assert text.startswith("event: done\n") and '"state": "done"' in text and text.count("event:") == 1


def test_lists_and_status(api: TestClient) -> None:
    headers = bearer()
    for prompt in ("a red fox", "a 100% blue_fox"):
        api.post("/v1/jobs", json={**BODY, "prompt": prompt}, headers=headers)
        run_worker()
    running = api.post("/v1/jobs", json=BODY, headers=headers).json()
    listed = api.get("/v1/jobs", headers=headers).json()["jobs"]
    assert listed[0]["id"] == running["id"] and len(listed) == 3  # what's still waiting comes first
    images = api.get("/v1/images", headers=headers).json()["images"]
    assert len(images) == 2
    assert [i["prompt"] for i in api.get("/v1/images", params={"search": "100%"}, headers=headers).json()["images"]] == ["a 100% blue_fox"]
    assert api.get("/v1/images", params={"search": "_"}, headers=headers).json()["images"][0]["prompt"] == "a 100% blue_fox"
    assert api.get("/v1/images", params={"search": "%"}, headers=headers).json()["images"][0]["prompt"] == "a 100% blue_fox"
    assert api.get("/v1/images", params={"search": "nothing"}, headers=headers).json()["images"] == []
    assert len(api.get("/v1/images", params={"limit": 1, "offset": 1}, headers=headers).json()["images"]) == 1
    assert api.get("/v1/images", params={"limit": 1000}, headers=headers).status_code == 422
    assert api.get("/v1/images", params={"offset": 10**30}, headers=headers).status_code == 422
    status = api.get("/v1/status", headers=headers).json()
    assert status["jobs"]["done"] == 2 and status["jobs"]["queued"] == 1 and status["worker"]["running"] is False
    known = api.get("/v1/models", headers=headers).json()
    assert known["default"] == catalog.DEFAULT.id and len(known["models"]) == len(catalog.MODELS)
    assert known["models"][0]["license"] == "Apache 2.0" and known["models"][0]["installed"] is False


def test_the_api_serves_and_deletes_only_files_in_the_images_folder(api: TestClient, tmp_path: Path) -> None:
    # mia generate --output can put an image anywhere, and a record's path must not reach outside.
    outside = tmp_path / "keep.png"
    record = generate(Request("x", 1, 512, 512, 1), catalog.DEFAULT, FakeEngine(), outside)
    headers = bearer()
    assert outside.exists()
    assert api.get(f"/v1/images/{record.id}/file", headers=headers).status_code == 404
    assert api.delete(f"/v1/images/{record.id}", headers=headers).status_code == 204
    assert outside.exists()
    assert history.get(paths.history_file(), record.id) is None


def test_deleting_an_image_takes_the_prompt_with_it(api: TestClient) -> None:
    # Its job held the same prompt, which stayed readable after the image was gone.
    headers = bearer()
    job = api.post("/v1/jobs", json=BODY, headers=headers).json()
    run_worker()
    image = api.get(f"/v1/jobs/{job['id']}", headers=headers).json()["image"]
    assert api.delete(f"/v1/images/{image}", headers=headers).status_code == 204
    assert api.get(f"/v1/jobs/{job['id']}", headers=headers).status_code == 404
    assert "a red fox" not in api.get("/v1/jobs", headers=headers).text


def test_event_streams_are_limited(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct, because a stream held open through the test client would block this test.
    monkeypatch.setattr(server, "MAX_STREAMS_PER_DEVICE", 2)
    monkeypatch.setattr(server, "MAX_STREAMS", 3)
    streams = server.Streams()
    assert streams.take("a") and streams.take("a")
    assert not streams.take("a")
    assert streams.take("b")
    assert not streams.take("c")  # the overall limit
    streams.give_back("a")
    assert streams.take("a")


def test_deleting_an_image_removes_its_file(api: TestClient) -> None:
    record = generate(Request("x", 1, 512, 512, 1), catalog.DEFAULT, FakeEngine())
    headers = bearer()
    assert api.delete(f"/v1/images/{record.id}", headers=headers).status_code == 204
    assert not Path(record.file).exists()
    assert api.get(f"/v1/images/{record.id}", headers=headers).status_code == 404


@pytest.mark.parametrize("bad", ["..", "..%2f..%2fetc", "ABCDEF123456", "abc"])
def test_odd_image_ids_are_simply_not_found(api: TestClient, bad: str) -> None:
    assert api.get(f"/v1/images/{bad}/file", headers=bearer()).status_code == 404


def test_the_interactive_docs_arent_served(api: TestClient) -> None:
    # They load scripts from a CDN. The OpenAPI document is enough, and holds nothing private.
    assert api.get("/docs").status_code == 404
    assert api.get("/redoc").status_code == 404
    assert api.get("/openapi.json").status_code == 200


def test_the_server_doesnt_load_the_engine() -> None:
    # The API must stay up when the engine can't load. Imported for real in a new process, so this also catches
    # anything reaching the engine through another module.
    code = (
        "import sys, mia.server; heavy = {'mlx', 'mflux', 'torch', 'PIL', 'mia.mflux_engine'} & set(sys.modules); "
        "print(sorted(heavy)); sys.exit(bool(heavy))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
