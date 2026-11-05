<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/mia-logo-dark.svg">
  <img src="images/mia-logo.svg" alt="mia logo: an amber sparkle beside tiles settling into place" width="88">
</picture>

# mia

[![tests](https://github.com/southsidedean/macos-ai-imagegen/actions/workflows/tests.yml/badge.svg)](https://github.com/southsidedean/macos-ai-imagegen/actions/workflows/tests.yml)

AI images on your own Mac: mia runs open models such as FLUX natively on Apple Silicon, and takes
requests from a web page, a REST API, an MCP server, or the command line. An AI assistant or a
script on another machine on your network can ask for a picture the same way you can at the Mac.

**It's early:** so far mia makes images from the command line and through a local REST API, on the Mac
itself. Remote access, the MCP server, and the web page are still to come.
[docs/roadmap.md](docs/roadmap.md) says where it stands.

## What it will do

- Run FLUX.2 klein 4B by default, or any other open model you add from Hugging Face.
- Keep one queue, so a request from your laptop waits its turn like one made at the Mac.
- Remember the prompt and settings of every image, so any of them can be made again.
- Keep your prompts and pictures private: it listens only on your Mac until you turn on remote access,
  and strips the metadata that would otherwise put your prompt and home folder in every image.
- Show each model's license before you download it, and refuse model files that can run code.

## What you can try today

You need an Apple Silicon Mac on macOS 14 or newer, and [uv](https://docs.astral.sh/uv/). The first
command installs the image engine, which takes a few GB with PyTorch. `mia models pull` then shows the
default model's license and asks before downloading it, about 4.6 GB.

```sh
git clone --branch v0.2.0 https://github.com/southsidedean/macos-ai-imagegen.git
cd macos-ai-imagegen
uv sync --locked --extra engine
uv run mia doctor
uv run mia models pull
uv run mia generate "a red fox in a snowy forest at sunrise, photograph"
```

It prints where it saved the image, in `~/Library/Application Support/mia/images`. A 1024 by 1024 image
took about 30 seconds on our test Mac. `uv run mia --help` lists the rest.

## Learn more

- [docs/api.md](docs/api.md): the REST API, with examples
- [docs/roadmap.md](docs/roadmap.md): what's done, and what's next
- [PLAN.md](PLAN.md): every decision, and the test results behind them
- [SECURITY.md](SECURITY.md): how to report a problem
- [CHANGELOG.md](CHANGELOG.md): what changed

## License

mia's code is under the [GNU AGPL 3.0](LICENSE). Each model has its own license, which may be
stricter: FLUX.1 dev, for example, is for non-commercial use only.
