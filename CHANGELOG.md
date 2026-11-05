# Changelog

## 0.4.0 (unreleased)

- AI assistants can make and find images through MCP, at `/mcp` or through `mia mcp`, using the same tokens and limits as
  the API. [The guide](https://github.com/southsidedean/macos-ai-imagegen/blob/main/docs/mcp.md) shows how to connect one.
- `mia service install` keeps mia running: it starts at login and comes back if it dies. `mia service status` and
  `mia service uninstall` go with it.
- `mia service stop`, `start`, and `restart`. `mia doctor` and `mia service status` say if the service is running an
  older version than the one installed.
- The Mac stays awake while a job runs, and can sleep again when it's done.
- A model that hasn't been used for 10 minutes gives back its memory, and the next job loads it again. `MIA_IDLE_SECONDS`
  changes the wait.
- The service's log stays small: above 10 MB, it moves aside, and only one old log is kept.
- Each new picture gets a small JPEG preview, at `GET /v1/images/{id}/preview`.
- Connections have limits. A request has 15 seconds to arrive, and connections are capped at 64 from one address
  and 256 overall.
- Every path but `/v1/health` and `/openapi.json` now needs a token, even one that doesn't exist, and the token is
  checked before a request's body is read.
- 30 refused requests in a minute from one address get a 429 until the minute has passed, even with a good token.
- `mia token create --days 7` makes a token with an end date. Tokens still last until revoked by default.
- A job whose step doesn't change for 30 minutes is marked failed, and the worker restarts for the next job.

## 0.3.0 (2026-11-04)

Scripts and other programs on your Mac can now make images, through a REST API and a queue. Reaching mia
from another machine comes in 0.4, with HTTPS and MCP.

- `mia serve` runs the API and a worker. [The API guide](https://github.com/southsidedean/macos-ai-imagegen/blob/v0.3.0/docs/api.md)
  shows how to use it.
- Jobs wait in a queue that survives a restart, and report progress as they run. They can be cancelled.
- The worker is a separate process. If it dies mid-job, the API stays up, the job is marked failed with the
  reason, and a new worker takes the next one.
- `mia token create` makes a token for each device, with its own permissions. `mia token revoke` stops one.
- Stopping the server fails the running job and keeps the waiting ones, which run when it starts again.
- Your 0.2 history moves forward by itself. `mia generate` now refuses to run while a worker holds the GPU,
  because two loaded models would use up the Mac's memory.

## 0.2.0 (2026-11-04)

The first version that makes images, from the command line. The queue, API, MCP, and web page come
next.

- `mia generate` makes an image with FLUX.2 klein 4B, or FLUX.1 schnell. It remembers the prompt and
  settings, saves no metadata in the picture, and `mia generate --again` makes one again and compares
  its pixels. Stopping a job keeps nothing.
- `mia models` lists the models, and `mia models pull` downloads one after showing its license and size.
- `mia history` lists recent images.
- It needs an Apple Silicon Mac on macOS 14 or newer, and Python 3.12 to 3.14. The image engine is an
  optional install, because it brings PyTorch: `uv sync --locked --extra engine`.
- `mia doctor` says whether a Mac is ready: its chip, Python, memory, disk, and image engine. It only
  reads, and exits with an error only for a problem that stops mia from working.
- The design tests are written up in `PLAN.md`, and they settled the default model (FLUX.2 klein 4B)
  and how the worker runs.
- The logo, the social preview, and the docs.
