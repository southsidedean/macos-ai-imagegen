# Plan: mia, an AI image generation stack for macOS

Status: written on 2026-11-04. 0.1 had no release of its own (decided 2026-11-04), and 0.2.0 is the
first release; `docs/roadmap.md` has where each release stands. Facts about outside projects were checked on that date and are listed under
[Sources](#sources). [The spike results](#spike-results-so-far) say what's been tested on a real Mac,
and what hasn't.

## Goal

Generate AI images on your own Mac, with open models, from anywhere you work. FLUX is the first
family mia supports, because it has already given good results, and any other open model on
Hugging Face can be added.

One queue on the Mac takes requests from four places:

- a web page, for sitting down and making pictures;
- a REST API, for scripts and other programs;
- an MCP server, so an AI assistant can make and fetch images;
- the `mia` command, for the terminal.

A request from a laptop or phone on your own network behaves the same as one made at the Mac.

## What we take from DiffusionBee

DiffusionBee is the example for how this should feel, and nothing more. mia doesn't read its
folders, use its model formats, or share any code with it.

Worth keeping:

- install it, pick a model, type a prompt, get a picture, with no knowledge of the internals;
- a history that remembers every prompt and setting, so any image can be made again;
- simple controls first, advanced ones a click away.

What it doesn't give us, and why mia exists: an API, an MCP server, remote requests, and a
direct way to use whichever open model you find on Hugging Face.

## Decisions

| Question | Decision |
|---|---|
| Name | **mia**, short for macos-ai-imagegen, the way kia is short for kodi-iptv-assistant. It's the name in the docs, on the pages, and on the command. The repository keeps its name. |
| Where it runs | Natively on one Apple Silicon Mac, as your user. Not in a container: Docker on macOS runs a Linux VM with no Metal access, so a container would run on the CPU. |
| Default engine | **mflux**, a native MLX implementation. It runs FLUX and takes Hugging Face repositories and LoRAs directly. It moves quickly, with a release most weeks, so Renovate's monthly update is how mia keeps up. It's pinned to an exact version and used only behind mia's engine interface, because its flags and classes change between releases. |
| Second engine | **diffusers** on PyTorch's MPS backend, from 0.6, for open models mflux doesn't implement (Stable Diffusion 1.5 and SDXL families, for example). It's an optional extra. mflux itself requires PyTorch today, so the extra adds diffusers, not PyTorch. |
| ComfyUI and Draw Things | Not engines for 1.0. ComfyUI's graph workflows are far more than this needs, and it runs on MPS rather than MLX. Draw Things has its own server and model library, which would put a second model store beside mia's. Either could become an engine later, if a real need appears. |
| Default models (decided 2026-11-04) | FLUX first; see [Models](#models). A fresh install offers a choice and downloads nothing until you pick. The default is FLUX.2 klein 4B, Apache 2.0 licensed. FLUX.1 schnell stays in the catalog, and FLUX.1 dev is in the catalog as an opt-in for personal use, with its non-commercial license shown. |
| Language and tools | Python, with `uv` and a committed lockfile with hashes: the ML dependency tree is large, and it has to be repeatable. uv also picks a Python version the ML packages support, whatever the system's newest Python is. |
| Web framework | FastAPI, so the API's OpenAPI document and its validation come from one set of types. kia and ok3d use only the standard library, but mia can't, since its engines need large dependencies anyway. Direct dependencies stay few, and each needs a reason. |
| MCP library | The official Python SDK, in the same server process as the API, rather than a handwritten server like kia's. The 2026-07-28 revision changed the protocol's core (it's now stateless), and the SDK should carry that kind of change for us. Version 2 of the SDK, with its `MCPServer` API, is the one to use; it answers both `2026-07-28` clients and older ones. |
| Processes | Two. `mia serve` runs the API, MCP, and web page. `mia worker` is the only process that loads a model. A crash or out-of-memory kill in the worker can't take the API down, the API never imports the ML packages, and restarting the worker is the sure way to hand its memory back. The worker keeps a model loaded between jobs, with MLX's cache capped at 2 GB, which was both the fastest and the steadiest way to run. After 10 idle minutes it exits and a new one starts with nothing loaded (decided 2026-11-05), because a service runs all the time and shouldn't hold a model's memory for a job that may never come. `MIA_IDLE_SECONDS` changes the wait, which is a first guess to adjust with use. Cancel takes effect at the next step, and a job still running 60 seconds later is killed along with the worker, which then restarts. `mia serve` starts and watches the worker itself, and launchd watches `mia serve`. `mia generate` and the worker share a lock on the GPU, so only one model is ever loaded. |
| Queue and history | One SQLite database, with images as PNG files beside it. History arrives in 0.2 and the queue in 0.3. One job runs at a time, because one model already uses most of the Mac's memory. Queued jobs survive a restart. A job that was running when the worker died is marked failed, with the reason, and isn't retried on its own. |
| Asynchronous by default | Submitting a request returns a job right away. Generation can take minutes, longer than HTTP and MCP clients usually wait, so a client submits, then polls, streams progress, or asks to wait a limited time. |
| Reproducible | Each image records its prompt, seed, size, steps, engine and MLX versions, and model revision. Hugging Face models are pinned to a commit, so "make it again" means the same weights. |
| Sleep | The worker holds a macOS power assertion (as `caffeinate -i` does) only while a job runs, so a remote request isn't cut off by idle sleep and the Mac can still sleep when idle. Waking a sleeping Mac for a request is out of scope. |
| Service | One launchd user agent for `mia serve`, installed by `mia service install` (changed from two on 2026-11-05). The supervisor already restarts the worker, stops a stuck job, and records why a job failed, none of which launchd can do, and a second agent would have split that between two programs. A user agent means someone must be logged in; the docs say so, and cover automatic login for a Mac used as a server. |
| Where things live | `~/Library/Application Support/mia/` by default (`MIA_HOME` moves it). The models folder has a setting of its own, so models can live on an external drive. A settings file is for later: for now, settings are environment variables and each command's options. |
| AI integration | A ladder, as in kia: `mia doctor` with no AI at all, then your own assistant through MCP, then an AI-assisted install prompt at the top of the README. mia holds no AI provider keys and makes no calls to an LLM; the assistant you connect does its own prompt writing. |
| Native app | Not before 1.0. The web page works on a phone and a laptop alike, without a separate app to build, sign, and notarize. |
| License (decided 2026-11-04) | AGPL-3.0, as in kia, ok3d, and arr-vpn-stack. This covers mia's code. Each model has its own license. |

## How it fits together

```text
 web page     scripts      AI assistant        terminal
    |            |              |                  |
    | HTTPS      | REST         | MCP              | mia (calls the API)
    v            v              v                  v
 +-----------------------------------------------------+
 | mia serve: tokens, limits, API, MCP, web page       |
 +--------------------------+--------------------------+
                            |
                  SQLite (jobs, images, models)
                            |
 +--------------------------v--------------------------+
 | mia worker: one job at a time, power assertion      |
 |   engine interface -> mflux (MLX), diffusers (MPS)  |
 +-----------------------------------------------------+
```

The fake engine implements the same interface and returns a small generated image with no ML
packages installed, so nearly all of mia can be tested on any machine.

## Models

### Catalog

The catalog is a small committed file of known-good models. Each entry names its Hugging Face
repository and commit, engine, license class, sizes, and suggested steps.

| Model | License | Role |
|---|---|---|
| FLUX.2 klein 4B | Apache 2.0 | The default, and the first model the tests and docs use |
| FLUX.1 schnell | Apache 2.0 | A different look, and slower. mflux says FLUX.1 may be deprecated |
| FLUX.1 dev | FLUX non-commercial license | Quality option at more steps. Personal use only; never downloaded without being chosen |
| FLUX.2 klein 9B | FLUX non-commercial license | Later option, same terms as dev |
| Z-Image Turbo | Apache 2.0, per secondary sources; confirm on the model card | Non-FLUX alternative already supported by mflux |

### Adding any Hugging Face model

`mia models add <org/repo>[@revision]`, the web page, and the API all do the same steps:

1. Read the repository's details with `huggingface_hub`, downloading no weights: its license,
   whether it's gated, its files and sizes, and its commit.
2. Identify the model: architecture, or for a LoRA the base model it needs, and the engine
   that can run it. If mia can't tell, it stops and says why.
3. Show what would happen: the license class, the download size, the free disk space, and
   whether it should fit in memory.
4. Download on confirmation, only weights and config files (tokenizer and chat templates included),
   at the pinned commit, resumable, with its progress shown like a job's. Every file left out is
   listed, because a skipped file otherwise shows up later as a confusing load error.
5. Try it with a small test image, then list it as ready. A model that fails stays listed, with
   the failure.

Rules that keep this safe:

- Weights must be safetensors. A `.ckpt`, `.bin`, or `.pt` file can run code when it's loaded,
  so mia refuses them, and offers no override.
- Code in a model repository never runs. No `trust_remote_code`, no custom pipelines, and code
  files aren't downloaded.
- The worker runs offline (`HF_HUB_OFFLINE=1`). Only the model manager downloads anything, so a
  job can't start an unexpected download.
- Every model shows its license class. Each is `open` (Apache, MIT), `open with terms`
  (OpenRAIL), `non-commercial`, or `unknown`. A setting can limit which classes can be added.
- Gated models need you to accept their terms on Hugging Face and supply your own token. mia
  stores it in the macOS Keychain, and no page, API, or MCP tool ever returns it.
- Disk comes first. A download that would leave less than a set amount free is refused before
  it starts.

LoRAs follow the same path and are checked against their base model. They're chosen per job, with a
strength.

### Model sizes

mflux's README lists FLUX.1 at about 34 GB unquantized, 18 GB at 8-bit, and 10 GB at 4-bit.
The `mflux-community` organization on Hugging Face publishes ready-quantized copies, ungated and
safetensors only, so neither a full download nor an account is needed to start. The 4-bit FLUX.1
schnell copy is 9.6 GB, and the 4-bit FLUX.2 klein 4B copy is 4.6 GB.

## Interfaces

### REST API

Versioned at `/v1`, with OpenAPI at `/openapi.json`.

```text
POST   /v1/jobs                  submit; returns 202 and a job (Idempotency-Key supported)
GET    /v1/jobs                  the queue and recent jobs
GET    /v1/jobs/{id}             status, step, and result
GET    /v1/jobs/{id}/events      progress as server-sent events
DELETE /v1/jobs/{id}             cancel
GET    /v1/images                history, with search
GET    /v1/images/{id}           settings and links
GET    /v1/images/{id}/file      PNG
GET    /v1/images/{id}/preview   a JPEG up to 512 pixels, which the worker makes with each image
DELETE /v1/images/{id}           delete, which needs the admin scope
GET    /v1/models                installed models and the catalog
POST   /v1/models/inspect        what adding a repository would do; changes nothing (0.6)
POST   /v1/models                add one, as a job (0.6)
DELETE /v1/models/{id}           remove one (0.6)
GET    /v1/health                liveness and version, no token needed, nothing private
GET    /v1/status                the worker, the queue, and free disk
```

An OpenAI-style `/v1/images/generations` endpoint is a possible later addition, so existing tools
can point at mia. It isn't in the plan until something needs it.

### MCP server

Streamable HTTP at `/mcp`, stateless, sharing the API's tokens, and `mia mcp` for stdio, which is a thin bridge
to the API. Both can run from a laptop to the Mac. Every tool calls the REST API, in process at `/mcp` and over the
network from `mia mcp`, so tokens, scopes, limits, and checks have one home.

| Tool | Scope | What it does |
|---|---|---|
| `mia_generate` | generate | Submits a job; optionally waits a limited time |
| `mia_job` | read | Status and progress, and the result when finished |
| `mia_cancel` | generate | Cancels a job |
| `mia_images` | read | Searches the history |
| `mia_image` | read | One image as a preview plus a link to the full file |
| `mia_models` | read | Installed models and the catalog |
| `mia_inspect_model` | read | What adding a Hugging Face repository would do (0.6, with that feature) |
| `mia_status` | read | The worker, the queue, and disk |

Choices worth recording:

- Base64 images use up an assistant's context, so tools return a small JPEG inline and a
  `resource_link` to the full PNG. The worker makes the JPEG, because the server can't load Pillow. Images from
  before 0.4 have no preview: nothing goes back to make them, and they still have the link.
- `mia_docs`, which was to search mia's docs, was dropped from 0.4 (decided 2026-11-05). The docs aren't installed
  with the package, and nothing has needed it yet.
- Submit and poll comes first, because it works in every client. The SDK has types for MCP's newer
  Tasks, which are adopted once clients in use support them, without removing the plain tools.
- Adding a model isn't a tool yet. An assistant can inspect a repository and tell you the
  command. A web page could suggest a download to an assistant, and a download can be many gigabytes,
  so a person makes that choice.
- Text from Hugging Face is untrusted. Model cards and repository names are returned as data,
  and the server's instructions say so.

### Web page

A prompt box, model picker, size, and Generate, with advanced settings folded away. Beside it: the
queue with live progress, and a gallery where any image can be reopened with its settings. It's plain
HTML, CSS, and JavaScript, with no build step, and uses only the public API. It works on a phone.

Choices worth recording:

- One event stream at a time, for the running job, and polling for the rest of the queue. mia speaks HTTP/1.1, and a browser
  allows about six connections to a host, so a stream per job could stop the page loading anything else.
- A gallery page is 24 pictures, loaded as they scroll into view. Previews stay `no-store`, so they count against the limit of 240
  requests a minute every time, and a picture refused by that limit tries again 30 seconds later.
- Asking with no token or cookie at all no longer counts as a refusal (decided 2026-11-06). It costs no database, and browsers do
  it by themselves: Safari asks for `/favicon.ico` and two `apple-touch-icon` files, and a signed-out page has to ask once, so a few
  reloads could otherwise have locked out an address.
- The number of pictures, up to 8, arrived in 0.5 (decided 2026-11-06), ahead of 0.7's batches, because it needs nothing new from
  the API: the page sends one job for each. So each can be stopped alone and keeps its own seed, and the existing limits apply. If
  the queue fills partway, the page says how many were added. Guidance and negative prompts aren't offered: klein accepts only
  guidance 1.0, and mflux's klein call takes no negative prompt.
- Tests run in real browsers (decided 2026-11-06), through Selenium, which is a development dependency only: Chrome and Firefox on
  Linux, and Safari on macOS. Playwright's WebKit isn't Safari, and its browsers are downloads with no hash in the lockfile.

## Security

Generated images and prompts are private, and this service will take requests over a network.

- Loopback by default. mia listens on `127.0.0.1` until you turn on remote access.
- Remote access starts with mia's own HTTPS on your LAN (decided 2026-11-04), using a certificate
  authority mia makes once, which each device trusts. It needs no extra software, and reaches only
  your own network. Tailscale Serve can follow for use away from home. Plain HTTP is never offered
  for remote use, and no path needs a port opened on the router.
- The authority's certificate limits it to `.local` and the names you chose, and it can't be limited
  by address (decided 2026-11-05). Limiting addresses as well passed OpenSSL and Apple's checks, then
  Claude Code refused every certificate holding an address, with `unsupported name constraint type`:
  BoringSSL doesn't do that check. Devices without Bonjour need addresses, so names alone wouldn't do.
  A stolen key could pose as a bare address, which matters far less than a site's name.
- HTTPS is TLS 1.3 only, and mia drops connections from outside this Mac and the private ranges,
  before a handshake. That rules out Tailscale's addresses for now, so that change has to widen it.
- Per-device tokens. `mia token create --for laptop --scope generate`. Scopes are `read`,
  `generate`, and `admin`. Only hashes are stored, tokens can be revoked one at a time, and they're
  accepted only in the `Authorization` header, never a URL. The web page signs in with one and keeps
  a session cookie, as [Browser sessions](#browser-sessions) describes.
- MCP's OAuth flow isn't built into mia. Its rules govern how OAuth tokens are handled, and a
  home server with static per-device tokens doesn't need an identity provider. If it's wanted later,
  it goes in front of mia as a gateway, as with agentgateway in kia.
- `Origin` is checked on every request, as the MCP spec requires against DNS rebinding. The page
  gets a strict content security policy.
- Limits on everything: requests per token, queue length, size, steps, batch count, prompt
  length, and uploads.
- Uploaded images are untrusted. They're checked for type and pixel count before decoding, and
  re-encoded to drop metadata.
- Prompts stay out of logs by default. mflux writes the prompt, the model's path, and the home folder
  into every image's EXIF and XMP data, so mia strips all of it by default (decided 2026-11-04),
  and a test proves it. Prompts and settings stay in mia's database, so any image can still be made
  again. A setting can write a small record with no paths into the PNG.

## Look and feel

The same family as kia, ok3d, and avs:

- `images/mia-logo.svg` and `images/mia-logo-dark.svg`: the 64-unit grid, with the rounded screen
  and stand, navy `#1f2b4d` (`#2b3a66` for dark mode), and pale `#cfd8f0`. On the screen, a
  row of tiles at uneven brightness, leaving room for a large amber `#e5a52e` sparkle: noise settling
  into an image (chosen 2026-11-04).
- `images/social-preview.html` and its rendered `social-preview.png`: 1280 by 640, on the navy
  radial background, in the same layout, with "mi" white and "a" amber, then a tagline and a line of
  explanation. It's uploaded in GitHub's settings by hand.
- README's top: logo, tagline, badges, then the AI-assisted install prompt and the quick start.
- Sample images in the docs are made by mia with an open model, with the prompt and settings
  beside each. Screenshots use a demo gallery, never your real history.

## Quality and CI

GitHub's hosted Mac runners can't run real generation in useful time, so CI proves everything apart
from the model, and real generation is proved by hand on a test Mac. Details of any one Mac stay out
of this repository, and `tests/test_privacy.py` checks this plan and `CLAUDE.md` for them.

Workflows, adapted from ok3d and kia:

- `tests.yml`, every pull request, push to main, and weekly:
  - lint and security: ruff, mypy (strict), bandit, shellcheck, zizmor, actionlint, Renovate's config
    check;
  - `pytest` on Linux (oldest supported Python and the newest the lockfile allows), using the fake
    engine;
  - `pytest (macOS)` on Apple Silicon, with the real engine's packages installed and checked for fit;
  - `uv lock --check` and a dependency vulnerability scan (pip-audit);
  - a smoke test in `tests/test_smoke.py` that starts the real server and worker with the fake engine,
    walks a job through the API, and kills the worker mid-job;
  - MCP in that smoke test, over HTTP in both protocol eras and through `mia mcp`;
  - the launchd agent's file, checked with `plutil` on macOS, against a stand-in for `launchctl`. The real agent can't
    run in CI, so `scripts/real-engine.sh` installs and removes one by hand.
- `codeql.yml`, `dependency-review.yml`, and `scorecard.yml`, as in ok3d.
- `release.yml`: on a tag, checks that it's signed and matches the code's version, builds the package,
  attests it, and publishes a release with an SBOM and notes from `CHANGELOG.md`.

Rules carried over:

- Actions pinned by commit, least-privilege permissions, and no persisted credentials on checkout.
- Renovate monthly, with a 7-day minimum release age; security fixes right away.
- `main` protected: pull requests, squash merges, required checks, signed release tags.
- `scripts/check.sh` runs what CI runs. `.githooks/` refuses commits on main, personal settings,
  secrets, private names, and images not looked at (`IMAGES_CHECKED=1`).
- Tests can't reach the network and have a 60-second limit. Links between our own docs are tested, and the
  docs have to name the version the code is at, but no test compares a number in the docs with the code. `tests/test_privacy.py` checks tracked files.
- No self-hosted runner. This repository is public, and a runner on a personal Mac would run
  code from pull requests next to private images and tokens. `scripts/real-engine.sh` runs by hand
  before each release and for any change to an engine, and the pull request says that it passed.
- `tests/test_repo.py` fails if any Claude asset other than `CLAUDE.md` and `PLAN.md` is tracked.

## Roadmap

The order puts what DiffusionBee can't do first: the API, MCP, and remote requests come before the
web page. `docs/roadmap.md` becomes the status page in 0.1.

| Release | What it brings | Done when |
|---|---|---|
| 0.1 Spike and foundation | The spike below; repository, CI, hooks, logo, social preview, `mia doctor` | Spike results are written into this plan, and CI is green with required checks on |
| 0.2 Engine and CLI | Engine interface, fake engine, mflux, FLUX from the catalog, `mia generate` | A FLUX image on a test Mac, reproduced from its recorded settings |
| 0.3 Queue and API | Worker, SQLite, REST API, per-device tokens, cancel, progress, limits | A worker killed mid-job leaves the API up and the failure recorded |
| 0.4 MCP and remote | MCP over HTTP and stdio, the launchd agent, LAN HTTPS with mia's certificate authority, power assertion | An assistant on another machine on the LAN makes and fetches an image, with the Mac idle |
| 0.5 Web page | The prompt page, queue, gallery, and sign-in | Generate and browse from a phone |
| 0.6 Hugging Face models | Inspect and add, LoRAs, license classes, Keychain token, disk checks; diffusers as a second engine | A third-party FLUX fine-tune, a LoRA, and an SDXL model all work; a pickle-only repository is refused |
| 0.7 Image to image | Image to image, inpainting, upscaling, batches, where the engines support them | Each works through the web page, API, and MCP |
| 0.8 Packaging | `install.sh` or a Homebrew tap, `mia upgrade` to signed releases only, backup and restore | A clean Mac reaches a first image using only the README |
| 1.0 | Full independent review of every file, no spot checks, plus an architectural review, with `docs/reviews/1.0-review.md` | Findings fixed or recorded |

### The 0.1 spike

Each answer goes back in this plan, and any that overturns a decision is called out.

1. Python: which versions do current mflux and MLX support? The system Python may be too new.
2. mflux: its license and exact version to pin, its Python API for FLUX, and whether one process
   can keep a model loaded across jobs and report each step's progress and cancel between steps.
3. Speed on a test Mac: time and peak memory for FLUX.1 schnell, FLUX.1 dev, and FLUX.2 klein 4B,
   at 4-bit and 8-bit, at 1024 by 1024. Published numbers for a given chip differ by about three
   times. The results pick the default model and quantization.
4. Quantized weights: are there ready-quantized FLUX repositories from a trustworthy source, or
   must mia quantize locally, and how much temporary disk does that take?
5. Gating: FLUX.1 schnell is Apache 2.0, yet its card asks for contact details before download.
   Does the first run need a Hugging Face account and token?
6. MLX under launchd: does a user agent get the GPU, including with the screen locked?
7. MCP: does the official SDK support the 2026-07-28 revision, and which clients in use need an
   older one? Do they support Tasks?

### Spike results so far

Measured on 2026-11-04. **mia ships mflux 0.24.0, mlx 0.32.4, and Pillow 13.0.0.** Most of these tests
ran on mflux 0.18.1 and an older mlx, and were then repeated where the answer could have changed: the
results below say which. mia's first pin was six releases behind, because the Python test below forced a
generic Mac platform and picked old versions, and nothing checked what was actually newest.

On the shipped versions, through mia's own engine (FLUX.2 klein 4B at 4-bit, 1024 by 1024, 4 steps):
about 29 seconds, and a peak memory footprint of 7.6 GB, down from about 24 GB. mflux 0.24 decodes in
strips, which it says takes about 1.6 GB instead of 7 to 14. FLUX.1 schnell took 54 seconds, with a 20
GB peak. Stopping a job took 3 to 6 seconds. The two images looked the same as before.

- Python (question 1): the engine installs on Python 3.12 to 3.14, and real generation gave identical
  pixels on 3.13 and 3.14. It doesn't install on 3.15, because sentencepiece has no wheels for it yet.
  mlx needs macOS 14 or newer. An earlier version of this plan said 3.12 and 3.13 only, which was wrong.
- mflux (question 2, in part): it's MIT licensed. mflux 0.18 crashed on Pillow 13, and holding Pillow
  back to avoid that left 39 known vulnerabilities, which `pip-audit` rightly refused. mflux 0.24
  requires Pillow 13 and fixes the crash, so neither a hold nor a workaround is needed.
- Speed (question 3, in part): FLUX.1 schnell at 4-bit, 1024 by 1024, 4 steps took 95 to 116 seconds
  on a test Mac, of which 52 to 62 were the denoising steps. Its peak memory footprint was about
  25.7 GB. That footprint may count memory-mapped weights, so it isn't yet a minimum memory figure.
- Quantized weights (question 4): `mflux-community` publishes them. See [Model sizes](#model-sizes).
- Gating (question 5): `black-forest-labs/FLUX.1-schnell` and FLUX.1 dev are gated, while
  `FLUX.2-klein-4B` and the `mflux-community` copies aren't. No account or token is needed to start.
- Reproducibility: the same seed, model, and settings gave identical pixels across runs. The files
  differ because of the metadata mflux adds, so mia compares pixels, not file hashes. Versions count too:
  mlx 0.29.3 and 0.31.2 gave visibly the same fox with different pixels, whichever code called them, and
  so did mflux 0.24. So mia pins MLX on its own, records both versions with each image, and a test fails
  if a pin moves without a new expected value in `scripts/real-engine.sh`.
- Older versions: the tables below were measured on mflux 0.18.1 and mlx 0.29.3, so their absolute
  numbers are out of date. The comparisons between models and between ways of running stand, but the
  default cache hasn't been compared with the capped one on the shipped versions.
- Default model (decided 2026-11-04): FLUX.2 klein 4B. mflux's docs say FLUX.1 gets maintenance fixes
  only and may be deprecated. With warm caches, both at 4-bit, 1024 by 1024, 4 steps:

  | | FLUX.2 klein 4B | FLUX.1 schnell |
  |---|---|---|
  | Total time | 42 to 52 s | 65 to 66 s |
  | Time per step | about 7.6 s | about 11.8 s |
  | Download | 4.6 GB | 9.6 GB |
  | Peak footprint | 24.1 GB | 25.6 GB |

  This rests on two prompts. Both spelled a short sign correctly. Schnell also invented a second
  sign full of made-up words.
- Image size and memory: klein 4B's peak footprint fell to about 10.3 GB at 512 by 512, so it follows
  image size more than download size. At 1024 by 1024, MLX reports a peak of about 13.7 GB in active
  use, and by default holds about 20 GB more in a reusable cache. That cache explains most of the
  footprint in the table above. A minimum memory figure still needs measuring.
- Keeping a model loaded (question 2): one process ran many jobs, and the pixels matched each other
  and a separate command-line run. Klein 4B at 1024 by 1024, four jobs in each mode, run twice for
  the default cache, on a Mac that was also doing other work:

  | How the worker runs | Time per job | Pageouts per job |
  |---|---|---|
  | Loaded, cache capped at 2 GB | 32 s, every job | 2 to 140 |
  | Loaded, no cache | 32 s, every job | 2 to 428 |
  | Fresh process per job | 39 to 44 s | 48 to 615 |
  | Loaded, default cache | 35 to 76 s | 228 to 1,259 |

  An earlier run with the default cache also had jobs of 110 and 160 seconds. The cap doesn't change
  the pixels. So the worker keeps the model loaded and caps the cache at 2 GB, which saves about 8
  seconds a job over a fresh process and avoids the slow outliers.
- Progress: mflux calls its step callback before MLX computes that step, because MLX evaluates lazily.
  The callback means a step has started, not finished, and the longest waits come after the last
  callback (the final step and the decode), so mia's progress has to allow for that.
- Cancel: raising an exception from the callback stops the job cleanly, and the next job's pixels were
  unchanged. It can only stop at a step boundary, and took 29 seconds in the one test, on a job
  running with the default cache. Killing a fresh process took 0.05 seconds at 20 seconds in, and left
  no image behind. A kill during the decode wasn't tested, because that job had already finished.
- Metadata: saving the plain image object writes no metadata, prompt, or home folder, with the same
  pixels. That's how mia strips it.
- launchd (question 6): a one-shot user agent in the GUI session ran in macOS's `Aqua` session on the
  GPU, and gave the same pixels as a direct run. It did the same with the screen locked for the whole
  job, at the same 7.5 seconds a step. A logged-out session wasn't tried: a user agent needs someone
  logged in, and the docs say so.
- MCP (question 7, in part): `mcp` 2.3.0 (MIT) has `2026-07-28` as its latest revision. Over its ASGI
  app in stateless JSON mode, a `2026-07-28` tool call returned an image and a resource link, a
  `2025-11-25` client could initialize, and it answered a bad `Origin` with 403 and a bad `Host` with
  421. Open SDK issues cover parts of the new revision, so it's pinned and tried with real clients
  in 0.4. Which clients need an older revision or support Tasks is still unknown.
- A real MCP client: Claude Code 2.1.311, on the same Mac, over both HTTP and `mia mcp`, with the real model. One
  `mia_generate` call that waited made the image, the assistant described the preview correctly, and it read the
  full PNG through its `mia://` link, which it saved to a file of its own. Both runs gave the known pixels. It never
  sent a chunked request, which the Guard refuses.

- The service: a real launchd agent, under a test label, gave the known pixels. `pmset` showed `caffeinate` holding the
  Mac awake during the job, and the process was gone after it. launchd brought back a killed server, which ran a job.

- Going idle: on a test Mac, a worker with klein 4B loaded had a footprint of about 4.1 GB between jobs, and a new one with
  nothing loaded had 17 MB. A job took 28 seconds with the model loaded and 30 after, with its weights still in the disk
  cache. A first job after the cache has gone wasn't timed. `ps` showed only 233 MB for the loaded worker, because a
  resident size leaves out most of what MLX holds for the GPU, so use `footprint` for this.
- The log: launchd opens it to append, tested with a real agent, so emptying it after a copy leaves no gap. A real restart
  moved a large log aside. mia does this itself because `newsyslog` needs root to configure, which mia otherwise doesn't.

Still open: other MCP clients, a real assistant running on another machine, FLUX.1 dev, the default cache against the capped one on the
shipped versions, and two things the service hasn't been through: a real restart and login, and a Mac actually reaching
its idle sleep time during a job.

### HTTPS on a real LAN

Tested on 2026-11-05, from a second machine on the same network, running Linux, with a throwaway `MIA_HOME`:

- The real model, requested and fetched over MCP through `mia mcp --ca` on that machine: done in 32 seconds, with the known
  pixels. The preview and the full PNG both arrived, and neither the prompt nor the token was in the server's log.
- From there: the authority's fingerprint matched, curl without it was refused, and plain HTTP got nothing on either port.
  No token gave 401, another `Host` or `Origin` 403, and TLS 1.2 was refused.
- Claude Code 2.1.311, on the Mac but connecting through its LAN address, called a tool directly with `NODE_EXTRA_CA_CERTS`
  and through `mia mcp --ca`, and failed without the authority. It's what found the limit on addresses described under Security.
- Not tested: a real assistant on the other machine (it didn't have one, so a scripted MCP client stood in), the macOS firewall
  turned on, a phone, and browsers, which the web page will need.
- 0.4 is accepted on this evidence (decided 2026-11-05). The exit test's wording asks for an assistant on the other machine. A
  scripted MCP client ran there, a real assistant ran on the Mac through its LAN address, and the pixels were the known ones, so
  each part has been shown. The exact combination should be tried when a machine with an assistant is to hand.
- "With the Mac idle" means awake, with nobody using it. A sleeping Mac can't answer and mia doesn't wake it, as decided above.
  The test ran under a hand-run `mia serve`, with the Mac awake and in use. `scripts/real-engine.sh` covers HTTPS under launchd,
  over loopback.

### What the review of 0.4 found

An independent review read every file, and reproduced what it could. All of this is fixed, with tests:

- With no token, requests sent together and never read held a connection for good: once a request has arrived, nothing timed
  an answer waiting to be read. Now 30 seconds with the buffers full closes it. Without that, 64 of them locked out an address.
  A pause in writing lasts until the kernel's send buffer drains, not until the client stops reading: on Linux, a reader at 20 KB a
  second paused for 29 seconds. So each connection's buffer is capped at 256 KB, which cut that to 5 seconds. Speed over a
  real LAN stayed at 47 MB a second, the same as with 4 MB.
- `GET /mcp` was a stream outside the limits that outlasted its token. It carries nothing in this mode, so it now answers 405.
- 30 bad tokens from this Mac stopped every device's MCP tools, as a tool's own calls shared this Mac's address.
- An unexpected error could end the HTTPS accept loop for good, silently. macOS can give no address for a reset connection.
- `mia service install` stopped the running service before refusing a busy port, and it and `restart` reported success
  when the server couldn't start. `mia doctor` crashed if another program held the port or the service's file was damaged.
- A problem with HTTPS took this Mac's own API down too. Now mia logs it and carries on for this Mac (decided 2026-11-05),
  with `mia doctor` and `mia remote status` saying it isn't listening.

Left as known limits, in `SECURITY.md`: a client reading just fast enough, any private network counting as yours, public
domains accepted by `--name`, and log noise from `Upgrade` requests. Also left: an MCP wait counts many times against the
limit, a hung worker can outlive a hand-run server killed with SIGKILL, and `scripts/real-engine.sh` can leave a process or
agent behind if it's interrupted.

### What 0.4 had to fix first

An independent review of 0.3 found no way past a token, and listed what wouldn't matter until the API left the Mac.
These were fixed before anything else in 0.4:

- Connections: a request has 15 seconds to arrive, however slowly it's sent, and connections are capped at 64 per
  address and 256 overall. uvicorn's own timer only covers the gap between requests.
- Tokens are checked in the Guard, before the body is read, for every path but the named public ones. So a route or
  mount added later, as MCP's may be, starts out closed.
- Refused requests are limited to 30 a minute per address, and past that they cost no database work. A token can't
  be guessed, so this is about the work, and a device on that address waits a minute with the rest.
- All tokens still share one queue and history, including over the network (decided 2026-11-05). Separating them
  would be user accounts, which are out of scope for 1.0, and the docs say to give tokens only to trusted devices.
  Tokens can now have an end date with `--days`, and last until revoked by default.
- A TLS handshake happens before any protocol sees a connection, where neither the deadline nor the caps reach, and uvicorn
  can't time one. So `server.Server` accepts HTTPS connections itself and uses asyncio's `connect_accepted_socket`: a
  handshake has 5 seconds, with 16 in progress from one address and 64 overall. On a real LAN, a silent connection was
  dropped at 5.0 seconds, and of 40 at once, 24 were dropped straight away.
- A job fails if its step doesn't change for 30 minutes, which restarts the worker. There's no limit on a whole job:
  its steps are capped already, and any fixed limit would either be too long to help or too short for a slow Mac.
  It uses a clock that stops while the Mac sleeps.

Still to do:

- Uploads in 0.7 need a higher size limit and more time to arrive, on their routes alone.

### Browser sessions

Decided on 2026-11-06, for the web page in 0.5. `<img>` and `EventSource` can't send an `Authorization` header, and a token kept
in the browser could be read by any script that got onto the page, so a browser swaps its token for a cookie.

- A session is 256 random bits, stored as a hash, with its token's scope. It lasts 90 days, or to the token's end date if that's
  sooner, and ends when the token does. A token is shown only once, so a phone shouldn't need it often. A session can't make
  another, or it could keep itself going for good. It's refused on `/mcp`.
- No separate CSRF token. `SameSite=Strict` isn't enough, because a site ignores ports: another program's page on `localhost:3000`
  counts as the same site as mia. So a cookie also needs `Sec-Fetch-Site: same-origin`, or `none` to read, and anything that writes
  needs an `Origin` exactly matching its scheme and `Host`. A page can't set either header, so a token in the page would add nothing.
  A request with neither header is refused, because every browser mia supports sends them. The Guard's looser `Origin` check still applies to tokens.
- Two cookie names: `__Host-mia_session` over HTTPS and `mia_session` on plain HTTP, each read only on its own scheme. Safari drops
  a `Secure` cookie on `http://127.0.0.1`, and Chrome and Firefox keep it, so one name that's always `Secure` would have failed
  in Safari alone. Tested on 2026-11-06 with Chrome 156, Firefox 159, and Safari on a test Mac, over `127.0.0.1` and `localhost`:
  each sent `Sec-Fetch-Site` and `Origin` as above on `fetch`, images, and `EventSource`.
- Sign-in stays a token pasted into the page (decided 2026-11-06). A QR code can follow if a real phone shows it's needed.

## Risks

| Risk | What we do about it |
|---|---|
| A dependency update breaks a fresh install, as Pillow 13 did on mflux 0.18 | Lockfile with hashes, no unpinned install path in the docs, and `pip-audit` in CI so a hold can't hide known vulnerabilities |
| mflux changes quickly, or stalls | Exact pin and one adapter; diffusers is a working alternative for FLUX |
| Memory pressure | Quantized defaults, MLX's cache capped, a memory check before loading, worker restarted when the model changes |
| Disk fills up | Refused before download, sizes shown first, external models folder |
| A leaked token | Scopes, per-device revocation, limits; no tool can reach files outside mia's folders |
| Malicious model files | Safetensors only, no remote code, no code files downloaded, worker offline |
| Licenses misread | Shown from the repository, `unknown` isn't treated as open, and terms are the user's call |

## Not in scope for 1.0

Video, model training, multiple Macs sharing a queue, cloud engines, Intel Macs, user accounts beyond
per-device tokens, and importing anything from DiffusionBee.

## Sources

Checked on 2026-11-04.

- [mflux](https://github.com/mflux-community/mflux) and its
  [releases](https://github.com/mflux-community/mflux/releases): MLX engine, Hugging Face repositories
  and LoRAs. Sizes from [a copy of its README](https://github.com/elitexp/mflux), to be rechecked.
- [Diffusers on MPS](https://huggingface.co/docs/diffusers/optimization/mps).
- [ComfyUI system requirements](https://docs.comfy.org/zh/installation/system_requirements): PyTorch
  MPS on Apple Silicon, not MLX.
- [Draw Things community repository](https://github.com/drawthingsai/draw-things-community/wiki).
- [FLUX.1 schnell](https://huggingface.co/black-forest-labs/FLUX.1-schnell),
  [FLUX.1 dev license](https://huggingface.co/black-forest-labs/FLUX.1-dev/blob/main/LICENSE.md), and
  [FLUX.2 klein 4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B).
- MCP 2026-07-28: [tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools),
  [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
  and [authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization).
