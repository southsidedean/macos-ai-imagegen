"""The mia command. Its output never includes secrets, and only `history` prints prompts back."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from mia import __version__, catalog, doctor, history, models, paths
from mia.engine import Cancelled, MiaError, Request, random_seed
from mia.generate import again, generate, load_engine

SIZE = 1024
EXIT_STOPPED = 130  # the shell's usual code for a job stopped by Ctrl-C


def _progress(step: int, total: int) -> None:
    # Reported as started, not finished: see Progress in engine.py.
    print(f"step {step} of {total}" + (", then finishing up" if step == total else ""), file=sys.stderr, flush=True)


def _stop(_signal: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _generate(args: argparse.Namespace) -> int:
    # launchd and scripts stop things with SIGTERM. Treat it like Ctrl-C, so no half picture is left.
    signal.signal(signal.SIGTERM, _stop)
    if args.again:
        remake = again(args.again)
        model, request = remake.model, remake.request
    else:
        if not args.prompt:
            raise MiaError("say what to make: mia generate \"a red fox in the snow\"")
        model = catalog.find(args.model)
        seed = random_seed() if args.seed is None else args.seed
        request = Request(args.prompt, seed, args.width, args.height, args.steps or model.steps)
    print(f"{model.title}, seed {request.seed}, {request.width} by {request.height}, {request.steps} steps", file=sys.stderr)
    engine = load_engine(model)
    start = time.perf_counter()
    record = generate(request, model, engine, args.output, _progress)
    print(f"{record.id}: done in {time.perf_counter() - start:.0f} seconds", file=sys.stderr)
    print(record.file)
    if args.again:
        same = record.pixels == remake.original.pixels
        print(f"identical pixels to {args.again}: {'yes' if same else 'no'}", file=sys.stderr)
        if not same:
            if record.engine_version != remake.original.engine_version:
                print(f"made with {record.engine} {record.engine_version}, not {remake.original.engine_version}", file=sys.stderr)
            return 1
    return 0


def _models_list() -> int:
    for model in catalog.MODELS:
        state = "ready" if models.is_installed(model, paths.models_folder()) else "not downloaded"
        default = ", default" if model is catalog.DEFAULT else ""
        print(f"{model.id}: {model.title} | {model.license} | {model.download_gb} GB | {state}{default}")
    return 0


def _models_pull(args: argparse.Namespace) -> int:
    model = catalog.find(args.model)
    folder = paths.models_folder()
    if models.is_installed(model, folder):
        print(f"{model.id} is already downloaded, in {models.folder_for(model, folder)}")
        return 0
    hub = models.HuggingFace()
    chosen = models.plan(model, hub)
    print(f"{model.title}\n  from {model.repo} at {model.revision[:10]}\n  license: {model.license} ({model.license_class})")
    print(f"  download: {chosen.size_gb:.1f} GB, to {models.folder_for(model, folder)}")
    if chosen.skipped:
        print(f"  left out: {', '.join(item.name for item in chosen.skipped)}")
    if not args.yes:
        if not sys.stdin.isatty():
            raise MiaError("nothing downloaded: add --yes to agree when running without a terminal")
        if input("Download it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing downloaded.")
            return 0
    print(f"ready, in {models.pull(model, folder, hub, chosen)}")
    return 0


def _history() -> int:
    for record in history.recent(paths.history_file()):
        print(f"{record.id}  {record.created}  {record.model}  seed {record.seed}  {record.prompt}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mia", description="Generate AI images on your own Mac.")
    parser.add_argument("--version", action="version", version=f"mia {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="is this machine ready? changes nothing")
    make = commands.add_parser("generate", help="make an image")
    make.add_argument("prompt", nargs="?", help="what to make")
    make.add_argument("--model", default=catalog.DEFAULT.id, help="default: %(default)s")
    make.add_argument("--seed", type=int, help="the same seed and settings give the same picture; default: random")
    make.add_argument("--width", type=int, default=SIZE, help="default: %(default)s")
    make.add_argument("--height", type=int, default=SIZE, help="default: %(default)s")
    make.add_argument("--steps", type=int, help="default: what the model suggests")
    make.add_argument("--output", type=Path, help="where to save the PNG; default: mia's images folder")
    make.add_argument("--again", metavar="ID", help="make an image from the history again, and compare its pixels")
    listing = commands.add_parser("models", help="the models mia knows")
    pulls = listing.add_subparsers(dest="action")
    pull = pulls.add_parser("pull", help="download a model")
    pull.add_argument("model", nargs="?", default=catalog.DEFAULT.id, help="default: %(default)s")
    pull.add_argument("--yes", action="store_true", help="don't ask first")
    commands.add_parser("history", help="recent images, with their prompts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor.run()
        if args.command == "generate":
            return _generate(args)
        if args.command == "history":
            return _history()
        return _models_pull(args) if args.action == "pull" else _models_list()
    except (Cancelled, KeyboardInterrupt) as stopped:
        print(f"Stopped, and no image was kept. {stopped}".strip(), file=sys.stderr)
        return EXIT_STOPPED
    except (MiaError, OSError) as problem:
        print(f"mia: {problem}", file=sys.stderr)
        return 1
