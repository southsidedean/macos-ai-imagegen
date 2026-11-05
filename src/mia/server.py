"""The REST API. It never imports an ML package: it reads and writes the database, and the worker does the rest.

Everything but PUBLIC needs a token, and the Guard checks it before anything else runs, so a route added later
can't be left open by accident. /mcp sits behind it too. It listens only on this Mac, unless mia remote on has added
HTTPS for your own network. Plain HTTP stays on this Mac alone.
"""

import asyncio
import contextlib
import json
import logging
import re
import resource
import shutil
import socket
import ssl
import sys
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

import h11
import httpx2
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request as HttpRequest, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field
from uvicorn.protocols.http.h11_impl import H11Protocol

from mia import __version__, catalog, history, jobs, mcp_server, models, paths, remote, service, tokens
from mia.engine import MAX_PROMPT, MiaError, Request, random_seed
from mia.generate import engine_is_fake
from mia.supervisor import Supervisor

HOST = "127.0.0.1"
PORT = 8765
# Only for HTTPS, once you've turned it on, and only addresses that are nearby get in.
ANYWHERE = "0.0.0.0"  # noqa: S104  # nosec B104
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
REFUSED_SIGN_INS_PER_MINUTE = 30
MAX_TRACKED = 10_000  # how many devices and addresses the limiter remembers: many addresses mustn't use up memory
MAX_CONNECTIONS, MAX_CONNECTIONS_PER_ADDRESS = 256, 64
ARRIVAL_SECONDS = 15.0  # how long a request has to arrive, from its first byte or from connecting
WRITE_STALL_SECONDS = 30.0  # how long a client can leave an answer unread while the buffer behind it is full
# A pause in writing lasts until the kernel's buffer has mostly drained, and it can grow by itself to several MB. Then a
# client reading steadily but slowly could look stalled. Capped, only one below about 10 KB a second can.
SEND_BUFFER_BYTES = 256 * 1024
# A handshake is the one thing a stranger can make mia do before any other limit applies. A device on a LAN needs moments.
HANDSHAKE_SECONDS = 5.0
MAX_HANDSHAKES, MAX_HANDSHAKES_PER_ADDRESS = 64, 16
TLS_CLOSE_SECONDS = 5.0  # asyncio waits 30 by default for the other side to agree to close
RENEW_CHECK_SECONDS = 6 * 3600  # a Mac used as a server can run for longer than its certificate lasts
PUBLIC = {("GET", "/v1/health"), ("GET", "/openapi.json"), ("HEAD", "/openapi.json")}
DEVICE = "mia.device"  # where the Guard leaves the device it signed in, in the request's scope
WITHIN = "mia.within"  # the address of an MCP tool's calls to this API, which no real connection can have
EVENT_POLL_SECONDS = 0.5
EVENT_KEEPALIVE_SECONDS = 15.0
EVENT_MAX_SECONDS = 3600.0
EVENT_TOKEN_CHECK_SECONDS = 5.0
MAX_STREAMS, MAX_STREAMS_PER_DEVICE = 32, 8
ID = re.compile(r"[0-9a-f]{12}")

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
logger = logging.getLogger("uvicorn.error")


class Connection(H11Protocol):
    """Limits what someone with no token can hold open. uvicorn only times a connection between requests, so one
    that never finishes sending a request would otherwise stay for as long as the sender likes."""

    _deadline: asyncio.TimerHandle | None = None
    _stall: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        transport.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
        address = self.client[0] if self.client else None
        from_here = [other for other in self.connections if isinstance(other, H11Protocol) and other.client and other.client[0] == address]
        if len(self.connections) > MAX_CONNECTIONS or len(from_here) > MAX_CONNECTIONS_PER_ADDRESS:
            transport.abort()
            return
        self._watch_arrival()

    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        self._watch_arrival()

    def connection_lost(self, exc: Exception | None) -> None:
        self._stop_watching()
        self._forget_stall()
        super().connection_lost(exc)

    def pause_writing(self) -> None:
        """A request that has arrived stops the deadline above, and no other timer runs while an answer waits to be
        read. So requests sent together and never read would hold the connection for good, even with no token."""
        super().pause_writing()
        if self._stall is None:
            self._stall = self.loop.call_later(WRITE_STALL_SECONDS, self.transport.abort)

    def resume_writing(self) -> None:
        self._forget_stall()
        super().resume_writing()

    def _forget_stall(self) -> None:
        if self._stall is not None:
            self._stall.cancel()
            self._stall = None

    def _watch_arrival(self) -> None:
        """One deadline for the whole request, which more data doesn't move: a byte at a time mustn't get around it."""
        if self.conn.their_state not in (h11.IDLE, h11.SEND_BODY):
            self._stop_watching()
        elif self._deadline is None:
            self._deadline = self.loop.call_later(ARRIVAL_SECONDS, self.transport.abort)

    def _stop_watching(self) -> None:
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None


class Guard:
    """Runs before everything else. Host and Origin checks are what stop DNS rebinding: a web page that points
    its own name at this Mac still can't get in, and MCP's spec requires the same check. The token check is here
    because FastAPI reads a body before a route's own checks, which would be work done for strangers."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], limiter: "Limiter", names: re.Pattern[str] | None = None) -> None:
        self.app = app
        self.limiter = limiter
        self.names = names

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        pairs = [(key.decode("latin1").lower(), value.decode("latin1")) for key, value in scope["headers"]]

        async def with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                sent = list(message.get("headers", []))
                sent.append((b"x-content-type-options", b"nosniff"))
                if not any(key.lower() == b"cache-control" for key, _ in sent):  # answers can hold prompts
                    sent.append((b"cache-control", b"no-store"))
                message["headers"] = sent
            await send(message)

        # The names you chose count only over HTTPS, so nothing can use them to reach the plain HTTP on this Mac.
        refusal = self._refuse(scope["method"], pairs, self.names if scope.get("scheme") == "https" else None)
        if refusal is None and (scope["method"], scope["path"]) not in PUBLIC:
            refusal = await self._sign_in(scope, pairs)
        if refusal is not None:
            await refusal(scope, receive, with_headers)
            return
        await self.app(scope, receive, with_headers)

    async def _sign_in(self, scope: Scope, pairs: list[tuple[str, str]]) -> JSONResponse | None:
        """A token can't be guessed, so the limit on refusals is there to stop strangers making mia do work. It comes
        first, so an address past it costs no database. A device on that address waits with the rest, for a minute."""
        address = scope["client"][0] if scope.get("client") else "unknown"
        # A tool's own calls were signed in on the way to /mcp, and all share one address, so they don't count: bad tokens
        # sent by something on this Mac would otherwise stop the tools of every device.
        counted = address != WITHIN
        if counted and self.limiter.full(address, "refused", REFUSED_SIGN_INS_PER_MINUTE):
            return JSONResponse({"detail": "too many refused requests"}, status.HTTP_429_TOO_MANY_REQUESTS, {"Retry-After": "60"})
        given = [value for key, value in pairs if key == "authorization"]
        scheme, _, presented = (given[0] if len(given) == 1 else "").partition(" ")
        found = await asyncio.to_thread(tokens.verify, presented.strip()) if scheme.lower() == "bearer" else None
        if found is None:
            if counted:
                self.limiter.allow(address, "refused", REFUSED_SIGN_INS_PER_MINUTE)
            return JSONResponse({"detail": "a valid token is needed"}, status.HTTP_401_UNAUTHORIZED, {"WWW-Authenticate": "Bearer"})
        if not self.limiter.allow(found.id, "all", REQUESTS_PER_MINUTE):
            return JSONResponse({"detail": "too many requests"}, status.HTTP_429_TOO_MANY_REQUESTS, {"Retry-After": "60"})
        scope[DEVICE] = found
        return None

    @staticmethod
    def _refuse(method: str, pairs: list[tuple[str, str]], names: re.Pattern[str] | None = None) -> JSONResponse | None:
        hosts = [value for key, value in pairs if key == "host"]
        if len(hosts) != 1 or not (ALLOWED_HOST.fullmatch(hosts[0]) or (names and names.fullmatch(hosts[0]))):
            return JSONResponse({"detail": "this Host isn't allowed"}, status.HTTP_403_FORBIDDEN)
        origins = [value for key, value in pairs if key == "origin"]
        named = names and origins and origins[0][:8].lower() == "https://" and names.fullmatch(origins[0][8:])
        if len(origins) > 1 or (origins and not (ALLOWED_ORIGIN.fullmatch(origins[0]) or named)):
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

    def full(self, device: str, kind: str, limit: int) -> bool:
        """Whether `allow` would refuse, without counting this as a request."""
        now = time.monotonic()
        with self._lock:
            times = self._seen.get((device, kind), ())
            return sum(1 for seen in times if now - seen <= 60) >= limit

    def allow(self, device: str, kind: str, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._seen) >= MAX_TRACKED:
                # Forgetting is safe: it only gives someone back a limit they could have waited a minute for.
                current = {key: seen for key, seen in self._seen.items() if seen and now - seen[-1] <= 60}
                self._seen = defaultdict(deque, current if len(current) < MAX_TRACKED else {})
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


def _names_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    chosen = "|".join(re.escape(name) for name in names)
    return re.compile(rf"(?:{chosen})(?::[0-9]{{1,5}})?", re.IGNORECASE) if chosen else None


def create_app(run_worker: bool = False, names: Iterable[str] = ()) -> FastAPI:
    limiter = Limiter()
    streams = Streams()
    supervisor = Supervisor() if run_worker else None

    assistants: list[MCPServer] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if supervisor:
            supervisor.start()
        try:
            async with assistants[0].session_manager.run():
                yield
        finally:
            if supervisor:
                supervisor.stop()

    # No /docs pages: they load scripts from a CDN, and this server shouldn't reach out or host them.
    app = FastAPI(title="mia", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(Guard, limiter=limiter, names=_names_pattern(names))

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

    def device(
        request: HttpRequest,
        _described: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))] = None,
    ) -> tokens.Device:
        """The device the Guard signed in. `_described` only puts the token in the OpenAPI document."""
        found: tokens.Device | None = request.scope.get(DEVICE)
        if found is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "a valid token is needed", {"WWW-Authenticate": "Bearer"})
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

    @app.get("/v1/images/{image_id}/preview")
    def get_image_preview(image_id: str, _device: Reader) -> FileResponse:
        path = paths.preview_file(_record(image_id).id)
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "that image has no preview")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.delete("/v1/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_image(image_id: str, _device: Admin) -> None:
        record = _record(image_id)
        path = _inside_images(record)
        if path is not None:
            path.unlink(missing_ok=True)
        paths.preview_file(record.id).unlink(missing_ok=True)
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

    # MCP's optional stream for messages from the server, which this server never sends. Left to the SDK, it stays open
    # with no limit, and outlasts its token. The spec lets a server answer 405, and clients carry on without it.
    @app.get("/mcp", include_in_schema=False)
    def no_stream(_device: Annotated[tokens.Device, Depends(device)]) -> Response:
        return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})

    # The tools call this same app with the assistant's own token, so the Guard and the routes check them as usual.
    # The Guard has already checked Host and Origin by the time /mcp runs, so there's one list of allowed names.
    within = httpx2.AsyncClient(transport=httpx2.ASGITransport(app, raise_app_exceptions=False, client=(WITHIN, 0)), base_url=f"http://{HOST}")
    assistant = mcp_server.build(within, lambda ctx: (ctx.headers or {}).get("authorization"))
    assistants.append(assistant)
    # Its route is added directly, because a mount would answer only at /mcp/ and send clients a redirect.
    app.router.routes.extend(
        assistant.streamable_http_app(
            json_response=True,
            stateless_http=True,
            max_request_body_size=MAX_BODY,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        ).routes
    )
    return app


def _raise_open_file_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = OPEN_FILES if hard == resource.RLIM_INFINITY else min(OPEN_FILES, hard)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


def _config(app: FastAPI) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        access_log=False,  # a search's query string holds text from prompts, and prompts stay out of logs
        log_level="warning",
        server_header=False,
        http=Connection,  # h11, the parser the Guard's checks were tested against: another could read a request differently
        proxy_headers=False,  # nothing sits in front of this, so a client's forwarded headers mean nothing
        ws="none",
        timeout_graceful_shutdown=SHUTDOWN_SECONDS,
    )


@dataclass(frozen=True)
class Secure:
    listener: socket.socket
    context: ssl.SSLContext
    settings: remote.Settings


class Server(uvicorn.Server):
    """uvicorn, plus the HTTPS listener. mia accepts those connections itself because asyncio does a handshake before
    any protocol sees a connection, where neither Connection's deadline nor its caps reach, and uvicorn can't set a time
    limit on one. Counting here, ahead of the handshake, covers that gap."""

    def __init__(self, config: uvicorn.Config, secure: Secure | None = None) -> None:
        super().__init__(config)
        self.secure = secure
        self._background: list[asyncio.Task[None]] = []
        self._handshakes: dict[asyncio.Task[None], str] = {}

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.secure:
            self.secure.listener.setblocking(False)
            self.secure.listener.listen(self.config.backlog)
            self._background = [asyncio.create_task(self._accept(self.secure)), asyncio.create_task(self._renew(self.secure))]

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        for task in (*self._background, *self._handshakes):
            task.cancel()
        if self.secure:
            self.secure.listener.close()
        await super().shutdown(sockets)

    async def _accept(self, secure: Secure) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                held, peer = await loop.sock_accept(secure.listener)
            except ConnectionAbortedError:  # the client gave up while waiting, which says nothing about this listener
                continue
            except OSError:  # out of files, most likely: wait for some to come back, as asyncio's own server does
                await asyncio.sleep(1)
                continue
            try:
                self._admit(held, peer, secure)
            except Exception:  # this loop ending would silently stop HTTPS, with everything else still looking fine
                logger.exception("mia couldn't take an HTTPS connection")
                held.close()

    def _admit(self, held: socket.socket, peer: Any, secure: Secure) -> None:
        # macOS can give no address for a connection that was reset while it waited.
        address = peer[0] if peer else None
        waiting = sum(1 for other in self._handshakes.values() if other == address)
        if address is None or not remote.nearby(address) or len(self._handshakes) >= MAX_HANDSHAKES or waiting >= MAX_HANDSHAKES_PER_ADDRESS:
            held.close()
            return
        task = asyncio.create_task(self._shake_hands(held, secure))
        self._handshakes[task] = address
        task.add_done_callback(self._handshakes.pop)

    async def _shake_hands(self, held: socket.socket, secure: Secure) -> None:
        def connection() -> asyncio.Protocol:
            return self.config.http_protocol_class(config=self.config, server_state=self.server_state, app_state=self.lifespan.state)  # type: ignore[call-arg]

        # OSError is a client that doesn't trust mia, doesn't speak TLS, or ran out of time. asyncio has closed it.
        with contextlib.suppress(OSError):
            await asyncio.get_running_loop().connect_accepted_socket(
                connection, held, ssl=secure.context, ssl_handshake_timeout=HANDSHAKE_SECONDS, ssl_shutdown_timeout=TLS_CLOSE_SECONDS
            )

    async def _renew(self, secure: Secure) -> None:
        """Compares the file with what's loaded, as mia remote on can also replace it. The settings are read each time,
        or this would put back names that were changed while it ran."""
        loaded = remote.certificate_serial()
        while True:
            await asyncio.sleep(RENEW_CHECK_SECONDS)
            try:
                settings = remote.load()
                if settings is None:
                    continue
                await asyncio.to_thread(remote.renew, settings)
                current = remote.certificate_serial()
                if current != loaded:
                    remote.reload(secure.context)
                    loaded = current
            except Exception as problem:  # the next check can try again, as long as this keeps running
                logger.warning("mia couldn't renew its HTTPS certificate: %s", problem)


def _bound(host: str, port: int) -> socket.socket:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OverflowError:
        listener.close()
        raise MiaError(f"{port} isn't a port: choose one up to 65535") from None
    except OSError as taken:
        listener.close()
        raise MiaError(f"port {port} is already in use, perhaps by another mia serve") from taken
    return listener


def _secure(port: int) -> Secure | None:
    """A problem with HTTPS leaves this Mac's own API running, and mia doctor and mia remote status say it isn't listening."""
    try:
        settings = remote.load()
        if settings is None:
            return None
        if settings.port == port:
            raise MiaError(f"HTTPS and this Mac's own HTTP can't share port {port}: run mia remote on --port with another")
        remote.renew(settings)
        context = remote.context()
        return Secure(_bound(ANYWHERE, settings.port), context, settings)
    except (MiaError, OSError, ValueError, TypeError) as problem:
        print(f"mia: HTTPS didn't start, so only this Mac can reach mia: {problem}", file=sys.stderr, flush=True)
        return None


def serve(port: int = PORT) -> int:
    service.rotate_log()  # here too: a server that keeps failing to start never reaches the supervisor's check
    _raise_open_file_limit()
    # Bound before anything starts: uvicorn runs the app's startup first, so a second mia serve on a taken port
    # would otherwise start a supervisor, fail, and shut it down beside the first server's worker.
    listener = _bound(HOST, port)
    secure = _secure(port)
    app = create_app(run_worker=True, names=secure.settings.hosts if secure else ())
    Server(_config(app), secure).run(sockets=[listener])
    return 0
