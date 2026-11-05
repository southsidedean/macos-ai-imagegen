"""One image, from request to recorded file. The queue and worker in 0.3 call this too."""

from __future__ import annotations

import contextlib
import os
import secrets
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mia import history, models, paths
from mia.catalog import Model, find
from mia.engine import Engine, MiaError, Progress, Request


def engine_is_fake() -> bool:
    """MIA_ENGINE=fake is for tests, and needs no ML packages or models."""
    return os.environ.get("MIA_ENGINE") == "fake"


def load_engine(model: Model) -> Engine:
    """The engine for a model, with it loaded."""
    if engine_is_fake():
        from mia.fake_engine import FakeEngine

        return FakeEngine()
    folder = models.folder_for(model, paths.models_folder())
    if not models.is_installed(model, paths.models_folder()):
        raise MiaError(f"{model.id} isn't downloaded yet: run mia models pull {model.id}")
    from mia.mflux_engine import MfluxEngine

    return MfluxEngine(model, folder)


def new_image_id() -> str:
    return secrets.token_hex(6)


def _never() -> bool:
    return False


def _quiet(_step: int, _total: int) -> None:
    pass


def _save_preview(engine: Engine, picture: Path, image_id: str) -> None:
    """The picture is what matters: without a preview, an assistant still gets its link, so this can't fail the job."""
    final = paths.preview_file(image_id)
    partial = final.with_name(f".{image_id}.partial.jpg")
    try:
        paths.make_private(final.parent)
        engine.preview(picture, partial)
        partial.replace(final)
    except Exception as problem:
        with contextlib.suppress(OSError, ValueError):
            print(f"couldn't make a preview: {type(problem).__name__}", file=sys.stderr, flush=True)
    finally:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)


def generate(
    request: Request,
    model: Model,
    engine: Engine,
    output: Path | None = None,
    progress: Progress = _quiet,
    cancelled: Callable[[], bool] = _never,
    then: Callable[[sqlite3.Connection, history.Record], None] | None = None,
    image_id: str | None = None,
) -> history.Record:
    image_id = image_id or new_image_id()
    in_our_folder = output is None
    if output is None:
        paths.make_private(paths.home_folder())
        paths.make_private(paths.images_folder())
        output = paths.images_folder() / f"{image_id}.png"
    elif output.suffix.lower() != ".png":
        raise MiaError(f"mia saves PNG files, so {output.name} needs to end in .png")
    else:
        output = output.expanduser().resolve()  # recorded, and later read from another folder
    # Written beside the final name and moved at the end, so a stopped job never leaves half a picture.
    partial = output.with_name(f".{image_id}.partial.png")
    try:
        pixels = engine.generate(request, partial, progress, cancelled)
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    if in_our_folder:  # the API can only reach pictures here, so one saved elsewhere has no use for a preview
        _save_preview(engine, output, image_id)
    record = history.Record(
        id=image_id,
        created=datetime.now(UTC).isoformat(timespec="seconds"),
        prompt=request.prompt,
        seed=request.seed,
        width=request.width,
        height=request.height,
        steps=request.steps,
        model=model.id,
        repo=model.repo,
        revision=model.revision,
        engine=engine.name,
        engine_version=engine.version,
        file=str(output),
        pixels=pixels,
    )
    history.add(paths.history_file(), record, then)
    return record


@dataclass(frozen=True)
class Again:
    original: history.Record
    model: Model
    request: Request


def again(image_id: str) -> Again:
    """What it takes to remake an image. Refuses a model whose pinned weights have since changed."""
    original = history.get(paths.history_file(), image_id)
    if original is None:
        raise MiaError(f"there's no image {image_id!r} in the history")
    model = find(original.model)
    if model.revision != original.revision:
        raise MiaError(f"{image_id} was made with a different revision of {model.id}, so it can't be remade exactly")
    request = Request(original.prompt, original.seed, original.width, original.height, original.steps)
    return Again(original, model, request)
