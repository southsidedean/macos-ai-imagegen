# Plan: mia, an AI image generation stack for macOS

Status: draft, written on 2026-11-04, before anything was built. Facts about outside projects
were checked on that date and are listed under [Sources](#sources). Anything still unproven is
marked as a question for the 0.1 spike.

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
| Default engine | **mflux**, a native MLX implementation. It runs FLUX and takes Hugging Face repositories and LoRAs directly. It's pinned to an exact version and used only behind mia's engine interface, because its flags and classes change between releases. |
| Second engine | **diffusers** on PyTorch's MPS backend, from 0.6, for open models mflux doesn't implement (Stable Diffusion 1.5 and SDXL families, for example). It's an optional extra, so a FLUX-only install never pulls in PyTorch. |
| ComfyUI and Draw Things | Not engines for 1.0. ComfyUI's graph workflows are far more than this needs, and it runs on MPS rather than MLX. Draw Things has its own server and model library, which would put a second model store beside mia's. Either could become an engine later, if a real need appears. |
| Default models (decided 2026-11-04) | FLUX first; see [Models](#models). A fresh install offers a choice and downloads nothing until you pick. The default is Apache 2.0 licensed. FLUX.1 dev is in the catalog as an opt-in for personal use, with its non-commercial license shown. |
| Language and tools | Python, with `uv` and a committed lockfile with hashes: the ML dependency tree is large, and it has to be repeatable. uv also picks a Python version the ML packages support, whatever the system's newest Python is. |
| Web framework | FastAPI, so the API's OpenAPI document and its validation come from one set of types. kia and ok3d use only the standard library, but mia can't, since its engines need large dependencies anyway. Direct dependencies stay few, and each needs a reason. |
| MCP library | The official Python SDK, in the same server process as the API, rather than a handwritten server like kia's. The 2026-07-28 revision changed the protocol's core (it's now stateless), and the SDK should carry that kind of change for us. |
| Processes | Two. `mia serve` runs the API, MCP, and web page. `mia worker` is the only process that loads a model. A crash or out-of-memory kill in the worker can't take the API down, the API never imports the ML packages, and restarting the worker is the sure way to hand its memory back. |
| Queue and history | One SQLite database, with images as PNG files beside it. One job runs at a time, because one model already uses most of the Mac's memory. Queued jobs survive a restart. A job that was running when the worker died is marked failed, with the reason, and isn't retried on its own. |
| Asynchronous by default | Submitting a request returns a job right away. Generation can take minutes, longer than HTTP and MCP clients usually wait, so a client submits, then polls, streams progress, or asks to wait a limited time. |
| Reproducible | Each image records its prompt, seed, size, steps, engine version, and model revision. Hugging Face models are pinned to a commit, so "make it again" means the same weights. |
| Sleep | The worker holds a macOS power assertion (as `caffeinate -i` does) only while a job runs, so a remote request isn't cut off by idle sleep and the Mac can still sleep when idle. Waking a sleeping Mac for a request is out of scope. |
| Service | Two launchd user agents, installed by `mia service install`. A user agent means someone must be logged in; the docs say so, and cover automatic login for a Mac used as a server. |
| Where things live | `~/Library/Application Support/mia/` by default (`MIA_HOME` moves it). The models folder has a setting of its own, so models can live on an external drive. Settings are in `settings.toml`, and the repository carries a commented `settings.example.toml`. |
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
| FLUX.1 schnell | Apache 2.0 | Fast default, and the first model the tests and docs use |
| FLUX.2 klein 4B | Apache 2.0 | Newer candidate; the 0.1 spike decides whether it's the default |
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
4. Download on confirmation, only weights and config files, at the pinned commit, resumable,
   with its progress shown like a job's.
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
Quantizing locally would seem to need the full weights first, while ready-quantized repositories
save that download if they can be trusted. The spike checks this.

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
GET    /v1/images/{id}/file      PNG (?size= for a smaller copy)
DELETE /v1/images/{id}           delete, which needs the admin scope
GET    /v1/models                installed models and the catalog
POST   /v1/models/inspect        what adding a repository would do; changes nothing
POST   /v1/models                add one, as a job
DELETE /v1/models/{id}           remove one
GET    /v1/health                liveness and version, no token needed, nothing private
GET    /v1/status                queue, memory, disk, loaded model
```

An OpenAI-style `/v1/images/generations` endpoint is a possible later addition, so existing tools
can point at mia. It isn't in the plan until something needs it.

### MCP server

Streamable HTTP at `/mcp`, sharing the API's tokens, and `mia mcp` for stdio, which is a thin bridge
to the API. Both can run from a laptop to the Mac.

| Tool | Scope | What it does |
|---|---|---|
| `mia_generate` | generate | Submits a job; optionally waits a limited time |
| `mia_job` | read | Status and progress, and the result when finished |
| `mia_cancel` | generate | Cancels a job |
| `mia_images` | read | Searches the history |
| `mia_image` | read | One image as a preview plus a link to the full file |
| `mia_models` | read | Installed models and the catalog |
| `mia_inspect_model` | read | What adding a Hugging Face repository would do |
| `mia_status` | read | Queue, memory, disk |
| `mia_docs` | read | Searches mia's own docs |

Choices worth recording:

- Base64 images use up an assistant's context, so tools return a small JPEG inline and a
  `resource_link` to the full PNG.
- Submit and poll comes first, because it works in every client. MCP's newer Tasks support is
  adopted if the spike finds that clients in use support it, without removing the plain tools.
- Adding a model isn't a tool yet. An assistant can inspect a repository and tell you the
  command. A web page could suggest a download to an assistant, and a download can be many gigabytes,
  so a person makes that choice.
- Text from Hugging Face is untrusted. Model cards and repository names are returned as data,
  and the server's instructions say so.

### Web page

A prompt box, model picker, size, and Generate, with advanced settings folded away. Beside it: the
queue with live progress, and a gallery where any image can be reopened with its settings. It's plain
HTML, CSS, and JavaScript, with no build step, and uses only the public API. It works on a phone.

## Security

Generated images and prompts are private, and this service will take requests over a network.

- Loopback by default. mia listens on `127.0.0.1` until you turn on remote access.
- Remote access starts with mia's own HTTPS on your LAN (decided 2026-11-04), using a certificate
  authority mia makes once, which each device trusts. It needs no extra software, and reaches only
  your own network. Tailscale Serve can follow for use away from home. Plain HTTP is never offered
  for remote use, and no path needs a port opened on the router.
- Per-device tokens. `mia token create --for laptop --scope generate`. Scopes are `read`,
  `generate`, and `admin`. Only hashes are stored, tokens can be revoked one at a time, and they're
  accepted only in the `Authorization` header, never a URL. The web page signs in with one and keeps
  a secure session cookie.
- MCP's OAuth flow isn't built into mia. Its rules govern how OAuth tokens are handled, and a
  home server with static per-device tokens doesn't need an identity provider. If it's wanted later,
  it goes in front of mia as a gateway, as with agentgateway in kia.
- `Origin` is checked on every request, as the MCP spec requires against DNS rebinding. The page
  gets a strict content security policy.
- Limits on everything: requests per token, queue length, size, steps, batch count, prompt
  length, and uploads.
- Uploaded images are untrusted. They're checked for type and pixel count before decoding, and
  re-encoded to drop metadata.
- Prompts stay out of logs by default, and PNG metadata is a setting, so that sharing a picture
  needn't share its prompt.

## Look and feel

The same family as kia, ok3d, and avs:

- `images/mia-logo.svg` and `images/mia-logo-dark.svg`: the 64-unit grid, with the rounded screen
  and stand, navy `#1f2b4d` (`#2b3a66` for dark mode), and pale `#cfd8f0`. On the screen, a landscape
  picture with an amber `#e5a52e` sparkle.
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
  - `pytest (macOS)` on Apple Silicon;
  - `uv lock --check` and a dependency vulnerability scan (pip-audit);
  - joining in the releases that bring what they check: launchd plist generation and install scripts
    and an import of the real engine packages on macOS (0.2 and 0.4), and a smoke test that starts the
    service with the fake engine and walks a job through the API and MCP (0.3 and 0.4).
- `codeql.yml`, `dependency-review.yml`, and `scorecard.yml`, as in ok3d.
- `release.yml`, added with the first release: on a signed tag, builds the package, attests it, and
  attaches an SBOM.

Rules carried over:

- Actions pinned by commit, least-privilege permissions, and no persisted credentials on checkout.
- Renovate monthly, with a 7-day minimum release age; security fixes right away.
- `main` protected: pull requests, squash merges, required checks, signed release tags.
- `scripts/check.sh` runs what CI runs. `.githooks/` refuses commits on main, personal settings,
  secrets, private names, and images not looked at (`IMAGES_CHECKED=1`).
- Tests can't reach the network and have a 60-second limit. Docs links and the docs' agreement with
  the code are tested. `tests/test_privacy.py` checks tracked files.
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
| 0.4 MCP and remote | MCP over HTTP and stdio, launchd agents, LAN HTTPS with mia's certificate authority, power assertion | An assistant on another machine on the LAN makes and fetches an image, with the Mac idle |
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

## Risks

| Risk | What we do about it |
|---|---|
| mflux changes quickly, or stalls | Exact pin and one adapter; diffusers is a working alternative for FLUX |
| Memory pressure | Quantized defaults, a memory check before loading, worker restarted when the model changes |
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
