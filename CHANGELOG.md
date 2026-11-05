# Changelog

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
