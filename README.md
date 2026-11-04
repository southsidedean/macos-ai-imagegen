<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/mia-logo-dark.svg">
  <img src="images/mia-logo.svg" alt="mia logo: an amber sparkle beside tiles settling into place" width="88">
</picture>

# mia

[![tests](https://github.com/southsidedean/macos-ai-imagegen/actions/workflows/tests.yml/badge.svg)](https://github.com/southsidedean/macos-ai-imagegen/actions/workflows/tests.yml)

AI images on your own Mac: mia runs open models such as FLUX natively on Apple Silicon, and takes
requests from a web page, a REST API, an MCP server, or the command line. An AI assistant or a
script on another machine on your network can ask for a picture the same way you can at the Mac.

**mia can't generate images yet.** It's early: the design is tested and written down, and the first
working version comes in 0.2. [docs/roadmap.md](docs/roadmap.md) says where it stands.

## What it will do

- Run FLUX.2 klein 4B by default, or any other open model you add from Hugging Face.
- Keep one queue, so a request from your laptop waits its turn like one made at the Mac.
- Remember the prompt and settings of every image, so any of them can be made again.
- Keep your prompts and pictures private: it listens only on your Mac until you turn on remote access,
  and strips the metadata that would otherwise put your prompt and home folder in every image.
- Show each model's license before you download it, and refuse model files that can run code.

## What you can try today

`mia doctor` says whether a Mac is ready for mia. It only reads, and it needs no model. You need
[uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/southsidedean/macos-ai-imagegen.git
cd macos-ai-imagegen
uv sync --locked
uv run mia doctor
```

## Learn more

- [docs/roadmap.md](docs/roadmap.md): what's done, and what's next
- [PLAN.md](PLAN.md): every decision, and the test results behind them
- [SECURITY.md](SECURITY.md): how to report a problem
- [CHANGELOG.md](CHANGELOG.md): what changed

## License

mia's code is under the [GNU AGPL 3.0](LICENSE). Each model has its own license, which may be
stricter: FLUX.1 dev, for example, is for non-commercial use only.
