"""Where mia keeps things. Nothing here creates a folder: doctor must be able to look without changing."""

from __future__ import annotations

import os
from pathlib import Path


def home_folder() -> Path:
    """MIA_HOME, or the usual macOS place."""
    chosen = os.environ.get("MIA_HOME")
    if chosen:
        return Path(chosen).expanduser()
    return Path.home() / "Library" / "Application Support" / "mia"


def models_folder() -> Path:
    """MIA_MODELS, so models can live on an external drive, or a folder under mia's own."""
    chosen = os.environ.get("MIA_MODELS")
    if chosen:
        return Path(chosen).expanduser()
    return home_folder() / "models"


def images_folder() -> Path:
    return home_folder() / "images"


def history_file() -> Path:
    return home_folder() / "history.sqlite3"


def make_private(folder: Path) -> None:
    """Makes a folder only you can read: it holds your prompts and pictures.

    mkdir's mode covers only the last folder, and only when it's new, so this sets it either way.
    """
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)


def previews_folder() -> Path:
    return images_folder() / "previews"


def preview_file(image_id: str) -> Path:
    return previews_folder() / f"{image_id}.jpg"
