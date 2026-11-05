"""The REST API. It never imports an ML package: it reads and writes the database, and the worker does the rest.

Every route but /v1/health and the OpenAPI document needs a token. It listens only on this Mac for now:
remote access arrives in 0.4, with HTTPS. Before then, idle connections need a time limit: a local process can
still hold many open with no token, though the raised open-file limit makes that harder.
"""

import asyncio
import json
import re
import resource
import shutil
import socket
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from mia import __version__, catalog, history, jobs, models, paths, tokens
from mia.engine import MAX_PROMPT, MiaError, Request, random_seed
from mia.generate import engine_is_fake
from mia.supervisor import Supervisor

HOST = "127.0.0.1"
PORT = 8765
# Exact matches only. Loose parsing would let "localhost:x@example.com" through.
LOCAL_NAME = r"(?:127\.0\.0\.1|localhost|\[::1\])"
ALLOWED_HOST = re.compile(rf"{LOCAL_NAME}(?::[0-9]{{1,5}})?", re.IGNORECASE)
ALLOWED_ORIGIN = re.compile(rf"https?://{LOCAL_NAME}(?::[0-9]{{1,5}})?", re.IGNORECASE)
MAX_BODY = 64 * 1024
MAX_OFFSET = 10**9
OPEN_FILES = 4096  # macOS starts a process with 256, which idle connections can use up
SHUTDOWN_SECONDS = 5  # how long an open event stream can hold up stopping the server
MIN_FREE_BYTES = 2 * 1024**3  # below this, an image could fail halfway, or squeeze out everything else on the Mac
REQUESTS_PER_MINUTE = 240
SUBMITS_PER_MINUTE = 30
EVENT_POLL_SECONDS = 0.5
EVENT_KEEPALIVE_SECONDS = 15.0
EVENT_MAX_SECONDS = 3600.0
EVENT_TOKEN_CHECK_SECONDS = 5.0
MAX_STREAMS, MAX_STREAMS_PER_DEVICE = 32, 8
ID = re.compile(r"[0-9a-f]{12}")

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class Guard:
    """Runs before everything else. Host and Origin checks are what stop DNS rebinding: a web page that points
    its own name at this Mac still can't get in, and MCP's spec requires the same check."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        pairs = [(key.decode("latin1").lower(), value.decode("latin1")) for key, value in scope["headers"]]
        refusal = self._refuse(scope["method"], pairs)
        if refusal is not None:
            await refusal(scope, receive, send)
            return

        async def with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                sent = list(message.get("headers", []))
                sent.append((b"x-content-type-options", b"nosniff"))
                if not any(key.lower() == b"cache-control" for key, _ in sent):  # answers can hold prompts
                    sent.append((b"cache-control", b"no-store"))
                message["headers"] = sent
            await send(message)

        await self.app(scope, receive, with_headers)

    @staticmethod
    def _refuse(method: str, pairs: list[tuple[str, str]]) -> JSONResponse | None:
        hosts = [value for key, value in pairs if key == "host"]
        if len(hosts) != 1 or not ALLOWED_HOST.fullmatch(hosts[0]):
            return JSONResponse({"detail": "this Host isn't allowed"}, status.HTTP_403_FORBIDDEN)
        origins = [value for key, value in pairs if key == "origin"]
        if len(origins) > 1 or (origins and not ALLOWED_ORIGIN.fullmatch(origins[0])):
            return JSONResponse({"detail": "this Origin isn't allowed"}, status.HTTP_403_FORBIDDEN)
        # A chunked body wins over Content-Length in the HTTP parser, so it would get past the limit below, and
        # FastAPI reads a body before it checks a token. Nothing that uses this API needs one.
        if any(key == "transfer-encoding" for key, _ in pairs):
            return JSONResponse({"detail": "a request body needs a Content-Length"}, status.HTTP_411_LENGTH_REQUIRED)
        if method in ("POST", "PUT", "PATCH"):
            length = next((value for key, value in pairs if key == "content-length"), "")
            if not length.isdigit():
                return JSONResponse({"detail": "a request body needs a Content-Length"}, status.HTTP_411_LENGTH_REQUIRED)
            if int(length) > MAX_BODY:
                return JSONResponse({"detail": "the request is too large"}, status.HTTP_413_CONTENT_TOO_LARGE)
        return None


class Limiter:
    """Counts requests per device in a sliding minute. MCP's spec asks for this, and a looping client shouldn't
    be able to keep mia busy. Lives in memory: a restart forgiving a device is fine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: defaultdict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, device: str, kind: str, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            times = self._seen[(device, kind)]
            while times and now - times[0] > 60:
                times.popleft()
            if len(times) >= limit:
                return False
            times.append(now)
            return True


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    prompt: Annotated[str, Field(max_length=MAX_PROMPT)]
    model: str = catalog.DEFAULT.id
    seed: int | None = None
    width: int = 1024
    height: int = 1024
    steps: int | None = None


def _job_view(job: jobs.Job) -> dict[str, Any]:
    view = asdict(job)
    for private in ("token", "idempotency_key", "planned_image"):
        del view[private]
    view["cancel_requested"] = job.cancel_requested is not None
    view["links"] = {"self": f"/v1/jobs/{job.id}", "events": f"/v1/jobs/{job.id}/events"}
    if job.image:
        view["links"]["image"] = f"/v1/images/{job.image}"
        view["links"]["file"] = f"/v1/images/{job.image}/file"
    return view


def _image_view(record: history.Record) -> dict[str, Any]:
    view = asdict(record)
    del view["file"]  # a path on this Mac: clients use the link
    view["links"] = {"self": f"/v1/images/{record.id}", "file": f"/v1/images/{record.id}/file"}
    return view


def _inside_images(record: history.Record) -> Path | None:
    """The image's file, if it's in mia's images folder. mia generate --output can put one anywhere, and the
    API must never become a way to read or delete a file elsewhere on the Mac."""
    path = Path(record.file).resolve()
    return path if path.is_relative_to(paths.images_folder().resolve()) else None


def _found[T](item: T | None, what: str) -> T:
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"there's no {what} with that id")
    return item


class Streams:
    """Counts open event streams, so a device can't hold every connection open."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: defaultdict[str, int] = defaultdict(int)

    def take(self, device: str) -> bool:
        with self._lock:
            if self._open[device] >= MAX_STREAMS_PER_DEVICE or sum(self._open.values()) >= MAX_STREAMS:
                return False
            self._open[device] += 1
            return True

    def give_back(self, device: str) -> None:
        with self._lock:
            self._open[device] -= 1


class _Stream(StreamingResponse):
    """Gives its slot back however it ends. A generator's own `finally` wouldn't run if a client hung up before
    the stream's first step, which would lose the slot for good."""

    def __init__(self, content: AsyncIterator[str], release: Callable[[], None], **settings: Any) -> None:
        super().__init__(content, **settings)
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


async def _events(job_id: str, device_id: str) -> AsyncIterator[str]:
    """Server-sent events: one when anything about the job changes, and the last when it's finished.

    Async, so a waiting stream doesn't hold one of the threads every other route needs. The database work goes to
    a thread for the moment it takes.
    """
    last: dict[str, Any] | None = None
    began = idle = checked = time.monotonic()
    while time.monotonic() - began < EVENT_MAX_SECONDS:
        if time.monotonic() - checked > EVENT_TOKEN_CHECK_SECONDS:
            checked = time.monotonic()
            if not await asyncio.to_thread(tokens.still_active, device_id):
                return
        job = await asyncio.to_thread(jobs.get, job_id)
        if job is None:
            return
        view = _job_view(job)
        if view != last:
            last, idle = view, time.monotonic()
            yield f"event: {'done' if job.state in jobs.FINISHED else 'progress'}\ndata: {json.dumps(view)}\n\n"
            if job.state in jobs.FINISHED:
                return
        elif time.monotonic() - idle > EVENT_KEEPALIVE_SECONDS:
            idle = time.monotonic()
            yield ": still working\n\n"
        await asyncio.sleep(EVENT_POLL_SECONDS)


def create_app(run_worker: bool = False) -> FastAPI:
    limiter = Limiter()
    streams = Streams()
    supervisor = Supervisor() if run_worker else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if supervisor:
            supervisor.start()
        try:
            yield
        finally:
            if supervisor:
                supervisor.stop()

    # No /docs pages: they load scripts from a CDN, and this server shouldn't reach out or host them.
    app = FastAPI(title="mia", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(Guard)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Any, problem: RequestValidationError) -> JSONResponse:
        # Without this, FastAPI echoes each bad value. A prompt that can't be encoded, such as half an emoji, then
        # fails the answer itself, and the server logs the whole prompt. Where and why is enough to fix a request.
        details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in problem.errors()]
        return JSONResponse({"detail": details}, status.HTTP_422_UNPROCESSABLE_CONTENT)

    @app.exception_handler(MiaError)
    async def _bad_request(_request: Any, problem: MiaError) -> JSONResponse:
        return JSONResponse({"detail": str(problem)}, status.HTTP_422_UNPROCESSABLE_CONTENT)

    @app.exception_handler(jobs.Conflict)
    async def _conflict(_request: Any, problem: jobs.Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(problem)}, status.HTTP_409_CONFLICT)

    @app.exception_handler(jobs.QueueFull)
    async def _full(_request: Any, problem: jobs.QueueFull) -> JSONResponse:
        return JSONResponse({"detail": str(problem)}, status.HTTP_429_TOO_MANY_REQUESTS, {"Retry-After": "60"})

    def device(authorization: Annotated[str | None, Header()] = None) -> tokens.Device:
        scheme, _, presented = (authorization or "").partition(" ")
        found = tokens.verify(presented.strip()) if scheme.lower() == "bearer" else None
        if found is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "a valid token is needed", {"WWW-Authenticate": "Bearer"})
        if not limiter.allow(found.id, "all", REQUESTS_PER_MINUTE):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests", {"Retry-After": "60"})
        return found

    def needs(scope: str) -> Callable[[tokens.Device], tokens.Device]:
        def check(found: Annotated[tokens.Device, Depends(device)]) -> tokens.Device:
            if not found.allows(scope):
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"this token's scope doesn't include {scope}")
            return found

        return check

    Reader = Annotated[tokens.Device, Depends(needs(tokens.READ))]
    Generator = Annotated[tokens.Device, Depends(needs(tokens.GENERATE))]
    Admin = Annotated[tokens.Device, Depends(needs(tokens.ADMIN))]

    @app.get("/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/status")
    def get_status(_device: Reader) -> dict[str, Any]:
        free = shutil.disk_usage(paths.home_folder()).free if paths.home_folder().exists() else None
        return {
            "version": __version__,
            "worker": {"running": supervisor is not None and supervisor.running()},
            "jobs": jobs.counts(),
            "disk_free_bytes": free,
        }

    @app.post("/v1/jobs", status_code=status.HTTP_202_ACCEPTED)
    def submit(
        body: JobIn,
        response: Response,
        found: Generator,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> dict[str, Any]:
        # A retry of a job that exists costs nothing, so nothing below should refuse it: not the quota, nor the
        # disk, nor a model removed since.
        if idempotency_key is not None:
            earlier = jobs.find_by_key(found.id, idempotency_key)
            if earlier is not None:
                model = catalog.find(body.model)
                steps = model.steps if body.steps is None else body.steps
                seed = earlier.seed if body.seed is None else body.seed
                if not earlier.same_request(Request(body.prompt, seed, body.width, body.height, steps), model.id):
                    raise jobs.Conflict("that Idempotency-Key was already used for a different request")
                response.headers["Location"] = f"/v1/jobs/{earlier.id}"
                return _job_view(earlier)
        if not limiter.allow(found.id, "submit", SUBMITS_PER_MINUTE):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many jobs submitted", {"Retry-After": "60"})
        model = catalog.find(body.model)
        if not engine_is_fake() and not models.is_installed(model, paths.models_folder()):
            raise jobs.Conflict(f"{model.id} isn't downloaded yet: run mia models pull {model.id}")
        if paths.home_folder().exists() and shutil.disk_usage(paths.home_folder()).free < MIN_FREE_BYTES:
            raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, "the Mac is nearly out of disk space")
        seed = random_seed() if body.seed is None else body.seed
        steps = model.steps if body.steps is None else body.steps
        request = Request(body.prompt, seed, body.width, body.height, steps)
        job, _new = jobs.submit(request, model.id, found.id, idempotency_key, seed_given=body.seed is not None)
        response.headers["Location"] = f"/v1/jobs/{job.id}"
        return _job_view(job)

    @app.get("/v1/jobs")
    def list_jobs(_device: Reader, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
        return {"jobs": [_job_view(job) for job in jobs.recent(limit)]}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, _device: Reader) -> dict[str, Any]:
        return _job_view(_found(jobs.get(job_id), "job"))

    @app.get("/v1/jobs/{job_id}/events")
    def job_events(job_id: str, found: Reader) -> _Stream:
        _found(jobs.get(job_id), "job")
        if not streams.take(found.id):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many open event streams", {"Retry-After": "60"})
        return _Stream(
            _events(job_id, found.id),
            lambda: streams.give_back(found.id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/v1/jobs/{job_id}", status_code=status.HTTP_202_ACCEPTED)
    def cancel_job(job_id: str, _device: Generator) -> dict[str, Any]:
        return _job_view(_found(jobs.cancel(job_id), "job"))

    @app.get("/v1/images")
    def list_images(
        _device: Reader,
        search: Annotated[str, Query(max_length=MAX_PROMPT)] = "",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    ) -> dict[str, Any]:
        found = history.recent(paths.history_file(), limit, offset, search)
        return {"images": [_image_view(record) for record in found]}

    def _record(image_id: str) -> history.Record:
        known = history.get(paths.history_file(), image_id) if ID.fullmatch(image_id) else None
        return _found(known, "image")

    @app.get("/v1/images/{image_id}")
    def get_image(image_id: str, _device: Reader) -> dict[str, Any]:
        return _image_view(_record(image_id))

    @app.get("/v1/images/{image_id}/file")
    def get_image_file(image_id: str, _device: Reader) -> FileResponse:
        path = _inside_images(_record(image_id))
        if path is None or not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "that image's file isn't in mia's images folder")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store"})

    @app.delete("/v1/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_image(image_id: str, _device: Admin) -> None:
        record = _record(image_id)
        path = _inside_images(record)
        if path is not None:
            path.unlink(missing_ok=True)
        history.delete(paths.history_file(), image_id)

    @app.get("/v1/models")
    def list_models(_device: Reader) -> dict[str, Any]:
        return {
            "default": catalog.DEFAULT.id,
            "models": [
                {
                    "id": model.id,
                    "title": model.title,
                    "license": model.license,
                    "license_class": model.license_class,
                    "download_gb": model.download_gb,
                    "steps": model.steps,
                    "installed": models.is_installed(model, paths.models_folder()),
                }
                for model in catalog.MODELS
            ],
        }

    return app


def _raise_open_file_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = OPEN_FILES if hard == resource.RLIM_INFINITY else min(OPEN_FILES, hard)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


def serve(port: int = PORT) -> int:
    import uvicorn

    _raise_open_file_limit()
    # Bound before anything starts: uvicorn runs the app's startup first, so a second mia serve on a taken port
    # would otherwise start a supervisor, fail, and shut it down beside the first server's worker.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((HOST, port))
    except OSError as taken:
        listener.close()
        raise MiaError(f"port {port} is already in use, perhaps by another mia serve") from taken
    config = uvicorn.Config(
        create_app(run_worker=True),
        access_log=False,  # a search's query string holds text from prompts, and prompts stay out of logs
        log_level="warning",
        server_header=False,
        http="h11",  # the parser the Guard's checks were tested against: another could read a request differently
        proxy_headers=False,  # nothing sits in front of this, so a client's forwarded headers mean nothing
        ws="none",
        timeout_graceful_shutdown=SHUTDOWN_SECONDS,
    )
    uvicorn.Server(config).run(sockets=[listener])
    return 0
