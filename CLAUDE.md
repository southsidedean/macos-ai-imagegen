# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## Roadmap

**mia** (short for macos-ai-imagegen) is a native macOS stack for generating AI images with open
models, FLUX first, through a web page, a REST API, an MCP server, and a command.

`PLAN.md` holds every decision, the roadmap to 1.0, and the CI rules. Read it
before starting work, and update it in the same pull request when a decision changes. Nothing is
built yet: the next step is the 0.1 spike in `PLAN.md`, and its answers go back into that file. From
0.1, `docs/roadmap.md` is the status page, and every pull request that starts, finishes, or changes a
roadmap item updates it.

DiffusionBee is only an example of how mia should feel. Never read its folders, use its model
formats, or depend on it.

## How it fits together

This is the design from `PLAN.md`; these parts arrive release by release.

- `mia serve` runs the REST API, the MCP server, and the web page. It never imports an ML package.
- `mia worker` is the only process that loads a model. It runs one job at a time, runs offline
  (`HF_HUB_OFFLINE=1`), and may crash or be killed without taking the API down. It keeps the model
  loaded with MLX's cache capped (`mx.set_cache_limit`): the default cache made jobs slow and uneven.
- mflux's step callback fires before MLX computes that step, so a callback means a step has started,
  not finished.
- They share one SQLite database, with images as files beside it, under `MIA_HOME` (by default
  `~/Library/Application Support/mia/`).
- Engines sit behind one interface: mflux (MLX) by default, diffusers (MPS) as an optional extra, and
  a fake engine for tests. Engine-specific code stays in its adapter, because mflux's API moves.
- Requests are asynchronous: submit, then poll or stream. Generation takes minutes, so don't add a
  blocking path.
- mia runs natively as launchd user agents. No containers: Docker on macOS has no Metal access.

## Commands

```sh
brew install uv shellcheck actionlint  # once: the tools the checks use
uv sync --locked --extra dev           # set up the environment from the lockfile
git config core.hooksPath .githooks    # once: the git hooks
scripts/check.sh                       # everything CI runs; the pre-push hook runs it too
uv run pytest -k name                  # tests by name
```

Still to come, with the releases that bring them: `scripts/real-engine.sh` (real generation, by
hand, on Apple Silicon only) and `mia doctor`.

- Don't run `ruff format`: as in the other repositories, ruff only lints.
- CI never runs a real model and there's no self-hosted runner. When a change touches an engine, run
  `scripts/real-engine.sh` and say in the pull request whether it passed.
- Actions are pinned by commit, and `tests/test_repo.py` checks it. A new required check goes in
  `main`'s branch protection in the same change.
- When piping test output, use `set -o pipefail`, so a failing test stops the chain.
- Tests can't reach the network and have 60 seconds each, unless marked otherwise.

## Rules for code

- Keep direct dependencies few, and give each a reason. Runtime dependencies come from the lockfile.
- Pin Hugging Face models to a commit, and engines to an exact version.
- Weights are safetensors only. Never add `trust_remote_code`, custom pipelines, or a way to load
  pickle files, not even behind a setting.
- Only the model manager downloads. A generation job must never start a download.
- Hugging Face text (model cards, names, tags) and uploaded images are untrusted input. An MCP tool
  returns such text as data.
- Adding or removing a model isn't an MCP tool. That choice stays with a person (`PLAN.md` says why).
- Tokens are accepted only in the `Authorization` header, stored only as hashes, and shown once. The
  Hugging Face token lives in the Keychain, and nothing returns it.
- Prompts are private: keep them out of logs by default.
- Every fix and feature gets a test. Comments explain why, in plain words.

## Models and licenses

Never describe a model as free or open without checking its model card for that exact checkpoint:
licenses attach to the checkpoint, not the family. FLUX.1 schnell and FLUX.2 klein 4B are Apache 2.0,
while FLUX.1 dev and klein 9B are non-commercial. Treat a license mia can't identify as `unknown`,
never as open.

## Git and GitHub

- `main` is protected: pull requests only, squash merges, no force-pushes. Actions are pinned by
  commit.
- One branch per change, `gh pr create`, wait for the checks, squash merge. Commit, merge, or push
  only when the user has said to.
- Stage files by name, never `git add -A`, and check `git status --short` first.
- Images: look at every one before committing, strip its metadata, and commit with
  `IMAGES_CHECKED=1`. Never commit real generated history; samples come with their prompt and
  settings, from an open model.
- Commit messages end with the Claude co-author line, and pull request bodies with the Claude Code
  line.
- Independent reviews: the brief says no spot checks, every file in scope read in full, and a coverage
  table first.
- Releases: user-facing changes go under the next version in `CHANGELOG.md`, marked unreleased.
  Release tags are signed; confirm with `git tag -v` before pushing one.

## Claude assets

`CLAUDE.md` and `PLAN.md` are the only Claude assets in this repository. Everything else (`.claude/`,
`.mcp.json`, `CLAUDE.local.md`, session files, working plans) is ignored by `.gitignore`, and is never
committed. Longer working notes go in the ignored `plans/` folder.

## Privacy (public repository)

No host names, domains, personal email addresses, home paths, LAN addresses, account names, tokens,
hardware details of a real machine (chip, memory, free disk), or real prompts and images in code, docs, screenshots, commit messages, pull request bodies, or
release notes. `CLAUDE.md` and `PLAN.md` are public too. Examples use stand-ins: `192.168.1.20`,
`192.0.2.x`, and `my-mac.lan`. The user's own names live in `.git/hooks/private-names`, never
committed. The user's machines belong in Claude's project memory, not this repository.

## Docs

- Readers have some computer skills but aren't experts: plain words, steps first, no padding.
- Lay docs out from easy to advanced: simplest path, options, advanced parts, then under the hood.
- Finish every docs edit with a pass using the `humanizer` skill. That covers `CHANGELOG.md`,
  comments in example settings, user-facing strings, and code comments.
- No em or en dashes. Check that examples are real before using them.
