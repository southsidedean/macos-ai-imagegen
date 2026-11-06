"""The REST API, in process, with the fake engine. test_smoke.py runs it for real."""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mia import catalog, db, history, jobs, paths, server, tokens
from mia.fake_engine import FakeEngine
from mia.generate import generate
from mia.engine import MiaError, Request
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
        ("GET", "/v1/images/abc/preview"),
        ("POST", "/mcp"),
        ("GET", "/mcp"),
        ("DELETE", "/v1/images/abc"),
        ("GET", "/v1/models"),
        ("POST", "/v1/session"),
        ("GET", "/v1/session"),
        ("DELETE", "/v1/session"),
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
    public = server.PUBLIC
    checked = 0
    for route in server.create_app().routes:
        if getattr(route, "path", "") == "/mcp":  # the SDK's route, proved in test_mcp.py: the Guard covers it
            continue
        # A mount or a websocket route has no methods: name it here, or fail.
        assert getattr(route, "methods", None), f"{route} isn't a route this test understands"
        for method in getattr(route, "methods", None) or ():
            path = getattr(route, "path", "")
            if (method, path) in public:
                continue
            assert isinstance(route, APIRoute), f"{method} {path} isn't an API route, so nothing checks a token"
            assert _needs_a_token(route.dependant), f"{method} {path} doesn't check a token"
            checked += 1
    assert checked >= 11  # the walk really did find the routes


def test_a_path_with_no_route_needs_a_token_too(api: TestClient) -> None:
    # The Guard is what refuses, so a mount or route added later starts out closed.
    assert api.get("/v1/nothing-here").status_code == 401
    assert api.get("/v1/nothing-here", headers=bearer()).status_code == 404
    assert api.get("/v1/health/").status_code == 401  # only the exact public paths are public


def test_the_token_is_checked_before_any_of_the_body_is_read() -> None:
    import asyncio

    async def app(_scope: Any, _receive: Any, _send: Any) -> None:
        raise AssertionError("the request got past the Guard")

    async def receive() -> dict[str, Any]:
        raise AssertionError("the Guard read the body")

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {
        "type": "http", "method": "POST", "path": "/v1/jobs", "query_string": b"", "client": ("192.0.2.1", 1),
        "headers": [(b"host", b"127.0.0.1"), (b"content-length", b"100")],
    }  # fmt: skip
    asyncio.run(server.Guard(app, server.Limiter())(scope, receive, send))
    assert sent[0]["status"] == 401


def test_refusals_carry_the_same_headers_as_answers(api: TestClient) -> None:
    for refused in (api.get("/v1/jobs"), api.get("/v1/health", headers={"Host": "evil.example"})):
        assert refused.headers["x-content-type-options"] == "nosniff"
        assert refused.headers["cache-control"] == "no-store"


def test_the_openapi_document_says_a_token_is_needed(api: TestClient) -> None:
    document = api.get("/openapi.json").json()
    assert document["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert document["paths"]["/v1/jobs"]["get"]["security"] == [{"HTTPBearer": []}]


def test_refused_requests_are_limited_per_address(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "REFUSED_SIGN_INS_PER_MINUTE", 3)
    good = bearer(tokens.READ)
    for _ in range(3):
        assert api.get("/v1/jobs", headers={"Authorization": f"Bearer mia_{'x' * 43}"}).status_code == 401
    limited = api.get("/v1/jobs")
    assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
    assert api.get("/v1/jobs", headers=good).status_code == 429  # the same address waits with the rest
    assert api.get("/v1/health").status_code == 200

    with TestClient(server.create_app(), base_url=BASE, client=("192.0.2.1", 1)) as elsewhere:
        assert elsewhere.get("/v1/jobs", headers=good).status_code == 200  # another address isn't affected


def test_good_requests_dont_count_as_refused(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "REFUSED_SIGN_INS_PER_MINUTE", 2)
    good = bearer(tokens.READ)
    for _ in range(5):
        assert api.get("/v1/jobs", headers=good).status_code == 200


def test_being_past_the_limit_costs_no_database(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from mia import db

    monkeypatch.setattr(server, "REFUSED_SIGN_INS_PER_MINUTE", 1)
    wrong = {"Authorization": f"Bearer mia_{'x' * 43}"}
    opened: list[object] = []
    real = db.connect

    def counted(path: Path | None = None) -> Any:
        opened.append(path)
        return real(path)

    monkeypatch.setattr(db, "connect", counted)
    assert api.get("/v1/jobs", headers=wrong).status_code == 401
    assert opened  # a token of the right shape does look
    opened.clear()
    assert api.get("/v1/jobs", headers=wrong).status_code == 429
    assert not opened


@pytest.mark.parametrize("value", ["mia_short", "other_" + "x" * 41, "mia_" + "!" * 43, "mia_" + "x" * 44])
def test_a_token_of_the_wrong_shape_costs_no_database(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from mia import db

    def fail(_path: object = None) -> None:
        raise AssertionError("opened the database")

    monkeypatch.setattr(db, "connect", fail)
    assert tokens.verify(value) is None


def test_two_authorization_headers_are_refused(api: TestClient) -> None:
    good = bearer(tokens.READ)["Authorization"]
    two = [("Authorization", good), ("Authorization", good)]
    assert api.get("/v1/jobs", headers=two).status_code == 401


def test_the_limiter_doesnt_grow_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_TRACKED", 10)
    limiter = server.Limiter()
    for number in range(100):
        limiter.allow(f"192.0.2.{number}", "refused", 5)
    assert len(limiter._seen) <= 10


def test_a_token_with_days_stops_working_when_they_run_out(api: TestClient) -> None:
    from mia import db

    issued = tokens.create("visitor", tokens.READ, days=7)
    headers = {"Authorization": f"Bearer {issued}"}
    device = next(d for d in tokens.devices() if d.name == "visitor")
    assert device.expires is not None and 6.9 * 86400 < device.expires - time.time() <= 7 * 86400
    assert api.get("/v1/jobs", headers=headers).status_code == 200
    assert tokens.still_active(device.id)
    with db.session() as connection:
        connection.execute("UPDATE tokens SET expires = ? WHERE name = 'visitor'", (time.time() - 1,))
    assert api.get("/v1/jobs", headers=headers).status_code == 401
    assert not tokens.still_active(device.id)  # an open event stream stops too


def test_a_token_lasts_until_revoked_unless_told_otherwise(api: TestClient) -> None:
    tokens.create("keeper")
    assert next(d for d in tokens.devices() if d.name == "keeper").expires is None


@pytest.mark.parametrize("days", [0, -1, tokens.MAX_DAYS + 1])
def test_a_tokens_days_have_a_range(days: int) -> None:
    with pytest.raises(MiaError, match="days"):
        tokens.create("visitor", days=days)
    assert not tokens.devices()


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
    assert api.get("/docs", headers=bearer()).status_code == 404
    assert api.get("/redoc", headers=bearer(name="other")).status_code == 404
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


def test_the_token_command_can_set_days_and_shows_them(capsys: pytest.CaptureFixture[str]) -> None:
    from mia.cli import main

    assert main(["token", "create", "--for", "visitor", "--days", "7"]) == 0
    assert "lasting 7 days" in capsys.readouterr().err
    assert main(["token", "create", "--for", "keeper"]) == 0
    capsys.readouterr()
    assert main(["token", "list"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert "expires " in listed[0] and "expire" not in listed[1]
    assert main(["token", "create", "--for", "bad", "--days", "0"]) == 1


# A browser's session. Python's client keeps cookies the way a browser does, unlike the Sec-Fetch and Origin headers,
# which each test sets to what real browsers send.
OWN_READ = {"Sec-Fetch-Site": "same-origin"}
OWN_WRITE = {"Sec-Fetch-Site": "same-origin", "Origin": BASE}


def signed_in(api: TestClient, scope: str = tokens.ADMIN, name: str = "phone") -> str:
    answer = api.post("/v1/session", headers=bearer(scope, name))
    assert answer.status_code == 201
    return api.cookies[server.COOKIE]


def test_signing_in_gives_a_cookie_only_the_server_can_read(api: TestClient) -> None:
    answer = api.post("/v1/session", headers=bearer(tokens.GENERATE, "phone"))
    assert answer.json() == {"device": "phone", "scope": "generate"}
    cookie = answer.headers["set-cookie"].lower()
    assert all(part in cookie for part in ("httponly", "samesite=strict", "path=/", "max-age=7776000"))
    assert "secure" not in cookie  # Safari drops a Secure cookie on plain HTTP, even on this Mac
    assert "domain" not in cookie


def test_over_https_the_cookie_is_secure_and_held_to_this_host() -> None:
    names = ["my-mac.local"]
    with TestClient(server.create_app(names=names), base_url="https://my-mac.local:8766") as client:
        answer = client.post("/v1/session", headers=bearer())
        cookie = answer.headers["set-cookie"]
        assert cookie.startswith("__Host-mia_session=")
        assert "Secure" in cookie
        assert "Domain" not in cookie
        own = {"Sec-Fetch-Site": "same-origin"}
        assert client.get("/v1/status", headers=own).status_code == 200
        # The plain name means nothing over HTTPS, where something on plain HTTP could have set it.
        client.cookies.clear()
        client.cookies.set(server.COOKIE, cookie.split("=", 1)[1].split(";")[0])
        assert client.get("/v1/status", headers=own).status_code == 401


def test_the_secure_cookie_means_nothing_over_plain_http(api: TestClient) -> None:
    value = signed_in(api)
    api.cookies.clear()
    api.cookies.set(server.SECURE_COOKIE, value)
    assert api.get("/v1/status", headers=OWN_READ).status_code == 401


def test_a_session_works_with_its_tokens_scope(api: TestClient) -> None:
    signed_in(api, tokens.READ)
    assert api.get("/v1/session", headers=OWN_READ).json() == {"device": "phone", "scope": "read"}
    assert api.get("/v1/jobs", headers=OWN_READ).status_code == 200
    assert api.post("/v1/jobs", json=BODY, headers=OWN_WRITE).status_code == 403


def test_a_session_can_submit_a_job_from_its_own_page(api: TestClient) -> None:
    signed_in(api, tokens.GENERATE)
    assert api.post("/v1/jobs", json=BODY, headers=OWN_WRITE).status_code == 202


@pytest.mark.parametrize(
    "headers",
    [
        {},  # not a browser mia supports, or not a browser
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},  # another program's page on this Mac: a site ignores ports
        {"Sec-Fetch-Site": "SAME-ORIGIN"},
    ],
)
def test_a_cookie_cant_read_unless_it_came_from_mias_page(api: TestClient, headers: dict[str, str]) -> None:
    signed_in(api)
    assert api.get("/v1/jobs", headers=headers).status_code == 403


def test_opening_a_link_can_read_with_a_cookie(api: TestClient) -> None:
    signed_in(api)
    assert api.get("/v1/jobs", headers={"Sec-Fetch-Site": "none"}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": BASE},  # no Sec-Fetch-Site
        {"Sec-Fetch-Site": "same-origin"},  # no Origin
        {"Sec-Fetch-Site": "none", "Origin": BASE},  # typing an address can only read
        {"Sec-Fetch-Site": "same-site", "Origin": BASE},
        {"Sec-Fetch-Site": "cross-site", "Origin": BASE},
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:3000"},  # the Guard allows this Origin for a token
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost:8765"},
        {"Sec-Fetch-Site": "same-origin", "Origin": "https://127.0.0.1:8765"},
    ],
)
def test_a_cookie_cant_write_unless_it_came_from_mias_page(api: TestClient, headers: dict[str, str]) -> None:
    signed_in(api)
    assert api.post("/v1/jobs", json=BODY, headers=headers).status_code == 403
    assert api.delete("/v1/images/abc", headers=headers).status_code == 403
    assert jobs.recent(10) == []


def test_the_page_checks_arent_needed_with_a_token(api: TestClient) -> None:
    assert api.get("/v1/jobs", headers={**bearer(), "Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_a_bad_token_doesnt_fall_back_on_a_cookie(api: TestClient) -> None:
    signed_in(api)
    wrong = {"Authorization": "Bearer mia_wrong", **OWN_READ}
    assert api.get("/v1/jobs", headers=wrong).status_code == 401


def test_two_session_cookies_are_refused(api: TestClient) -> None:
    value = signed_in(api)
    api.cookies.clear()
    headers = {"Cookie": f"{server.COOKIE}={value}; {server.COOKIE}={value}", **OWN_READ}
    assert api.get("/v1/jobs", headers=headers).status_code == 401


def test_a_session_is_found_among_other_cookies(api: TestClient) -> None:
    value = signed_in(api)
    api.cookies.clear()
    headers = {"Cookie": f"other=1; {server.COOKIE}={value}; another=2", **OWN_READ}
    assert api.get("/v1/jobs", headers=headers).status_code == 200


def test_a_session_cant_make_another(api: TestClient) -> None:
    # It would otherwise keep itself going for good, past the 90 days, with no token.
    signed_in(api)
    assert api.post("/v1/session", headers=OWN_WRITE).status_code == 403


def test_a_session_cant_reach_mcp(api: TestClient) -> None:
    signed_in(api)
    assert api.post("/mcp", json={}, headers=OWN_WRITE).status_code == 401
    assert api.get("/mcp", headers=OWN_READ).status_code == 401


def test_a_session_is_stored_only_as_a_hash(api: TestClient, tmp_path: Path) -> None:
    value = signed_in(api)
    for file in (tmp_path / "mia").glob("history*"):
        assert value.encode() not in file.read_bytes()


def test_signing_out_ends_the_session_and_clears_the_cookie(api: TestClient) -> None:
    value = signed_in(api)
    answer = api.delete("/v1/session", headers=OWN_WRITE)
    assert answer.status_code == 204
    assert 'mia_session=""' in answer.headers["set-cookie"]
    assert "max-age=0" in answer.headers["set-cookie"].lower()
    api.cookies.clear()
    assert api.get("/v1/jobs", headers={"Cookie": f"{server.COOKIE}={value}", **OWN_READ}).status_code == 401
    assert tokens.verify_session(value) is None


def test_signing_out_needs_the_page_too(api: TestClient) -> None:
    # Otherwise any site could sign you out.
    value = signed_in(api)
    assert api.delete("/v1/session", headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}).status_code == 403
    assert tokens.verify_session(value) is not None


def test_revoking_a_token_ends_its_sessions(api: TestClient) -> None:
    value = signed_in(api)
    tokens.revoke("phone")
    assert api.get("/v1/jobs", headers=OWN_READ).status_code == 401
    with db.session() as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    assert tokens.verify_session(value) is None


def test_a_revoked_token_stops_a_session_even_if_its_row_remains() -> None:
    # In case a session's row outlives its token: the token is what decides.
    tokens.create("phone")
    device = tokens.devices()[0]
    value, _seconds = tokens.start_session(device)
    with db.session() as connection:
        connection.execute("UPDATE tokens SET revoked = 'now' WHERE id = ?", (device.id,))
    assert tokens.verify_session(value) is None
    assert not tokens.still_active(device.id, value)


def test_a_session_stops_after_90_days(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens.create("phone")
    value, seconds = tokens.start_session(tokens.devices()[0])
    assert seconds == pytest.approx(90 * 86400, abs=2)
    now = time.time()
    monkeypatch.setattr("mia.tokens.time.time", lambda: now + 89 * 86400)
    assert tokens.verify_session(value) is not None
    monkeypatch.setattr("mia.tokens.time.time", lambda: now + 91 * 86400)
    assert tokens.verify_session(value) is None


def test_a_session_cant_outlast_its_token(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens.create("phone", days=7)
    device = tokens.devices()[0]
    value, seconds = tokens.start_session(device)
    assert seconds == pytest.approx(7 * 86400, abs=2)
    now = time.time()
    monkeypatch.setattr("mia.tokens.time.time", lambda: now + 8 * 86400)
    assert tokens.verify_session(value) is None


def test_old_sessions_are_cleared_and_each_token_has_a_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens.create("phone")
    device = tokens.devices()[0]
    first, _seconds = tokens.start_session(device)
    made = [tokens.start_session(device)[0] for _ in range(tokens.MAX_SESSIONS)]
    assert tokens.verify_session(first) is None  # the oldest went
    assert all(tokens.verify_session(value) is not None for value in made)
    now = time.time()
    monkeypatch.setattr("mia.tokens.time.time", lambda: now + 91 * 86400)
    tokens.start_session(device)
    with db.session() as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


@pytest.mark.parametrize("value", ["", "x", "a" * 42, "a" * 44, "a" * 42 + "!", "' OR 1=1 --"])
def test_a_session_of_the_wrong_shape_costs_no_database(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    def no_database(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the database was opened")

    monkeypatch.setattr("mia.tokens.db.session", no_database)
    assert tokens.verify_session(value) is None


def test_refused_cookies_count_as_refusals(api: TestClient) -> None:
    signed_in(api)
    for _ in range(server.REFUSED_SIGN_INS_PER_MINUTE):
        assert api.get("/v1/jobs", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert api.get("/v1/jobs", headers=OWN_READ).status_code == 429


def test_a_session_counts_against_its_tokens_limit(api: TestClient) -> None:
    signed_in(api)
    for _ in range(server.REQUESTS_PER_MINUTE - 1):  # signing in used one
        assert api.get("/v1/session", headers=OWN_READ).status_code == 200
    assert api.get("/v1/session", headers=OWN_READ).status_code == 429


def test_an_event_stream_stops_when_its_session_ends(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr(server, "EVENT_TOKEN_CHECK_SECONDS", 0)
    monkeypatch.setattr(server, "EVENT_POLL_SECONDS", 0.01)
    value = signed_in(api)
    job = api.post("/v1/jobs", json=BODY, headers=OWN_WRITE).json()
    device = tokens.devices()[0]

    async def read() -> list[str]:
        seen: list[str] = []
        async for event in server._events(job["id"], device.id, value):
            seen.append(event)
            tokens.end_session(value)
        return seen

    assert len(asyncio.run(asyncio.wait_for(read(), 5))) == 1  # one event, then the check ends it with the job still queued


def test_an_image_links_to_a_preview_only_if_it_has_one(api: TestClient) -> None:
    headers = bearer()
    api.post("/v1/jobs", json=BODY, headers=headers)
    run_worker()
    image = api.get("/v1/images", headers=headers).json()["images"][0]
    assert image["links"]["preview"] == f"/v1/images/{image['id']}/preview"
    assert api.get(image["links"]["preview"], headers=headers).status_code == 200
    paths.preview_file(image["id"]).unlink()
    assert "preview" not in api.get(f"/v1/images/{image['id']}", headers=headers).json()["links"]


def test_the_events_route_gives_the_stream_its_session(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The session's own check is above: this is what makes a real stream use it.
    given: list[str | None] = []

    async def stream(_job: str, _device: str, session: str | None = None) -> Any:
        given.append(session)
        yield ": nothing\n\n"

    monkeypatch.setattr(server, "_events", stream)
    value = signed_in(api)
    job = api.post("/v1/jobs", json=BODY, headers=OWN_WRITE).json()
    assert api.get(job["links"]["events"], headers=OWN_READ).status_code == 200
    assert given == [value]


# The page. tests/test_browser.py runs it for real. These are the rules that hold whatever it does.
WEB = Path(server.__file__).parent / "web"


@pytest.mark.parametrize("path", list(server.PAGE))
def test_the_page_needs_no_token_and_carries_its_policy(api: TestClient, path: str) -> None:
    answer = api.get(path)
    assert answer.status_code == 200
    assert answer.headers["content-type"].split(";")[0] == server.PAGE[path][1].split(";")[0]
    assert answer.headers["x-content-type-options"] == "nosniff"
    assert answer.headers["referrer-policy"] == "same-origin"
    policy = answer.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy and "'unsafe" not in policy


def test_nothing_else_in_the_pages_folder_or_beyond_it_is_served(api: TestClient) -> None:
    for path in ("/index.html", "/web/mia.js", "/../server.py", "/%2e%2e/server.py", "/mia.js/", "/server.py"):
        assert api.get(path).status_code == 401


def test_only_get_serves_the_page(api: TestClient) -> None:
    assert api.post("/", json={}).status_code == 401


def test_every_file_in_the_pages_folder_is_on_the_list() -> None:
    assert {file.name for file in WEB.iterdir()} == {name for name, _kind in server.PAGE.values()}


def test_the_page_loads_nothing_from_elsewhere_and_has_nothing_inline() -> None:
    page = (WEB / "index.html").read_text()
    assert "<style" not in page and " style=" not in page and "javascript:" not in page
    assert not re.search(r"<script(?![^>]*\bsrc=)", page)
    assert not re.search(r"\son[a-z]+=", page)
    for file in WEB.iterdir():
        if file.suffix != ".svg":  # a logo names its XML namespace, which is never fetched
            assert not re.search(r"(?:src|href)=[\"']?(?:https?:)?//|url\(|@import|https?://", file.read_text()), file.name


def test_the_script_never_builds_html_from_a_string() -> None:
    # Prompts come from any device with a token. Text in the DOM can't run, and the policy is the second line of defence.
    script = (WEB / "mia.js").read_text()
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc", "sessionStorage"):
        assert risky not in script, risky


def test_the_only_thing_the_page_keeps_is_the_forms_choices() -> None:
    # What's written is what the browser test reads back: never a token or a prompt.
    script = (WEB / "mia.js").read_text()
    assert re.findall(r"localStorage\.\w+\([^)]*", script) == ["localStorage.getItem(FORM_KEY", "localStorage.setItem(FORM_KEY, JSON.stringify(saved"]
    saved = re.search(r"const saved = (\{.*?\});", script)
    assert saved is not None and "prompt" not in saved[1] and "token" not in saved[1]


def test_the_sign_in_form_doesnt_offer_to_keep_the_token() -> None:
    assert re.search(r'<input id="token" type="password" autocomplete="off"', (WEB / "index.html").read_text())


@pytest.mark.parametrize(("ours", "original"), [("logo.svg", "mia-logo.svg"), ("logo-dark.svg", "mia-logo-dark.svg")])
def test_the_pages_logos_are_the_projects(ours: str, original: str) -> None:
    root = Path(__file__).resolve().parent.parent
    assert (WEB / ours).read_bytes() == (root / "images" / original).read_bytes()


def test_the_package_includes_the_page() -> None:
    settings = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert "web/*" in settings["tool"]["setuptools"]["package-data"]["mia"]


def test_asking_with_nothing_doesnt_count_as_a_refusal(api: TestClient) -> None:
    # A signed-out page has to ask once, and Safari asks for three icons by itself. Reloading mustn't lock anyone out.
    for _ in range(server.REFUSED_SIGN_INS_PER_MINUTE + 5):
        assert api.get("/favicon.ico").status_code == 401
    assert api.get("/v1/jobs", headers=bearer()).status_code == 200


def test_a_bad_cookie_counts_as_a_refusal(api: TestClient) -> None:
    good = bearer()
    for _ in range(server.REFUSED_SIGN_INS_PER_MINUTE):
        assert api.get("/v1/jobs", headers={"Cookie": f"{server.COOKIE}={'x' * 43}", **OWN_READ}).status_code == 401
    assert api.get("/v1/jobs", headers=good).status_code == 429
