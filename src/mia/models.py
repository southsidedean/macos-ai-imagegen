"""Getting models onto the Mac. This is the only part of mia that downloads anything.

A model's folder counts as installed only once its marker is written, after every wanted file has
been checked, so an interrupted download is never mistaken for a working model.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mia.catalog import Model
from mia.engine import MiaError

MARKER = ".mia-model.json"
GB = 1024**3
# A download that would leave less than this free is refused before it starts.
RESERVE_GB = 10
WEIGHTS = (".safetensors",)
# Everything else a model needs. Templates are text, and leaving one out shows up later as a confusing
# load error: FLUX.2 klein's chat_template.jinja did exactly that.
SUPPORT = (".json", ".txt", ".model", ".jinja", ".md")
# These can run code when loaded, so a repository that only offers these is refused, with no override.
PICKLES = (".ckpt", ".bin", ".pt", ".pth", ".pkl")


@dataclass(frozen=True)
class RepoFile:
    name: str
    size: int


class Hub(Protocol):
    def files(self, repo: str, revision: str) -> list[RepoFile]: ...

    def download(self, repo: str, revision: str, folder: Path, names: list[str]) -> None: ...


class HuggingFace:
    """Hugging Face, with no token: the catalog's models don't need one."""

    def files(self, repo: str, revision: str) -> list[RepoFile]:
        info = self._api().model_info(repo, revision=revision, files_metadata=True, token=False)
        return [RepoFile(item.rfilename, item.size or 0) for item in info.siblings or []]

    def download(self, repo: str, revision: str, folder: Path, names: list[str]) -> None:
        from huggingface_hub import snapshot_download

        snapshot_download(repo, revision=revision, local_dir=folder, allow_patterns=names, token=False)

    @staticmethod
    def _api() -> Any:
        try:
            from huggingface_hub import HfApi
        except ImportError as missing:
            raise MiaError("the image engine isn't installed: run uv sync --extra engine") from missing
        return HfApi()


def folder_for(model: Model, models_folder: Path) -> Path:
    return models_folder / model.repo.replace("/", "--")


def is_installed(model: Model, models_folder: Path) -> bool:
    """Whether this exact revision finished downloading."""
    try:
        marker = json.loads((folder_for(model, models_folder) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and marker.get("revision") == model.revision


def choose(files: list[RepoFile]) -> tuple[list[RepoFile], list[RepoFile]]:
    """Splits a repository into what to download and what to leave out. Code is never downloaded."""
    if not any(item.name.endswith(WEIGHTS) for item in files):
        offered = "pickle files, which can run code when loaded" if any(item.name.endswith(PICKLES) for item in files) else "no weights"
        raise MiaError(f"this repository has no safetensors weights, only {offered}; mia won't use it")
    wanted = [item for item in files if item.name.endswith(WEIGHTS + SUPPORT)]
    return wanted, [item for item in files if item not in wanted]


@dataclass(frozen=True)
class Plan:
    wanted: list[RepoFile]
    skipped: list[RepoFile]

    @property
    def size_gb(self) -> float:
        return sum(item.size for item in self.wanted) / GB


def plan(model: Model, hub: Hub) -> Plan:
    wanted, skipped = choose(hub.files(model.repo, model.revision))
    return Plan(wanted, skipped)


def _free_gb(folder: Path) -> float:
    for candidate in (folder, *folder.parents):
        if candidate.exists():
            return shutil.disk_usage(candidate).free / GB
    raise MiaError(f"can't read the free space for {folder}")


def pull(model: Model, models_folder: Path, hub: Hub, chosen: Plan) -> Path:
    """Downloads a planned model, resuming anything already there, then checks every file."""
    folder = folder_for(model, models_folder)
    have = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file()) / GB if folder.exists() else 0.0
    needed = max(chosen.size_gb - have, 0.0)
    free = _free_gb(folder)
    if free - needed < RESERVE_GB:
        raise MiaError(f"not enough disk: this needs {needed:.1f} GB and {free:.1f} GB is free, and mia keeps {RESERVE_GB} GB clear")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / MARKER).unlink(missing_ok=True)  # not installed again until it's been checked again
    hub.download(model.repo, model.revision, folder, [item.name for item in chosen.wanted])
    wrong = [item.name for item in chosen.wanted if not _matches(folder / item.name, item.size)]
    if wrong:
        raise MiaError(f"the download didn't finish: {', '.join(wrong)}. Run the same command to resume it")
    (folder / MARKER).write_text(json.dumps({"repo": model.repo, "revision": model.revision}) + "\n", encoding="utf-8")
    return folder


def _matches(path: Path, size: int) -> bool:
    return path.is_file() and path.stat().st_size == size
