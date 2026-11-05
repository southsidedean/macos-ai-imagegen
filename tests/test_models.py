"""The model manager and the catalog, with a stand-in for Hugging Face."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from mia import catalog, cli, models
from mia.engine import MiaError
from mia.models import Plan, RepoFile

MODEL = catalog.DEFAULT
FILES = [
    RepoFile(".gitattributes", 10),
    RepoFile("README.md", 5),
    RepoFile("tokenizer/chat_template.jinja", 4),
    RepoFile("tokenizer/tokenizer.json", 6),
    RepoFile("transformer/0.safetensors", 100),
    RepoFile("evil.py", 3),
    RepoFile("old.ckpt", 200),
]


class StandIn:
    def __init__(self, files: list[RepoFile] = FILES, write_short: str = "") -> None:
        self.listed = files
        self.write_short = write_short
        self.downloads: list[list[str]] = []

    def files(self, repo: str, revision: str) -> list[RepoFile]:
        assert (repo, revision) == (MODEL.repo, MODEL.revision)  # always the pinned commit
        return self.listed

    def download(self, repo: str, revision: str, folder: Path, names: list[str]) -> None:
        assert (repo, revision) == (MODEL.repo, MODEL.revision)
        self.downloads.append(names)
        sizes = {item.name: item.size for item in self.listed}
        for name in names:
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * (sizes[name] - (name == self.write_short)))


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "mia"))
    monkeypatch.delenv("MIA_MODELS", raising=False)
    monkeypatch.setattr(models, "_free_gb", lambda _folder: 500.0)


def test_only_weights_and_support_files_are_chosen() -> None:
    wanted, skipped = models.choose(FILES)
    assert [item.name for item in wanted] == [
        "README.md", "tokenizer/chat_template.jinja", "tokenizer/tokenizer.json", "transformer/0.safetensors",
    ]  # fmt: skip
    assert {item.name for item in skipped} == {".gitattributes", "evil.py", "old.ckpt"}


def test_a_pickle_only_repository_is_refused() -> None:
    with pytest.raises(MiaError, match="can run code"):
        models.choose([RepoFile("model.ckpt", 1), RepoFile("config.json", 1)])
    with pytest.raises(MiaError, match="no safetensors"):
        models.choose([RepoFile("README.md", 1)])


def test_pulling_checks_every_file_and_then_marks_it_installed(tmp_path: Path) -> None:
    hub = StandIn()
    folder = tmp_path / "models"
    assert not models.is_installed(MODEL, folder)
    result = models.pull(MODEL, folder, hub, models.plan(MODEL, hub))
    assert result == folder / "mflux-community--flux2-klein-4b-mflux-q4"
    assert models.is_installed(MODEL, folder)
    assert (result / "tokenizer" / "chat_template.jinja").exists()
    assert not (result / "evil.py").exists() and not (result / "old.ckpt").exists()


def test_a_short_download_isnt_installed_and_can_resume(tmp_path: Path) -> None:
    folder = tmp_path / "models"
    short = StandIn(write_short="transformer/0.safetensors")
    with pytest.raises(MiaError, match=r"didn't finish: transformer/0\.safetensors"):
        models.pull(MODEL, folder, short, models.plan(MODEL, short))
    assert not models.is_installed(MODEL, folder)
    whole = StandIn()
    models.pull(MODEL, folder, whole, models.plan(MODEL, whole))
    assert models.is_installed(MODEL, folder)


def test_another_revisions_marker_doesnt_count(tmp_path: Path) -> None:
    folder = models.folder_for(MODEL, tmp_path)
    folder.mkdir(parents=True)
    (folder / models.MARKER).write_text(json.dumps({"revision": "other"}), encoding="utf-8")
    assert not models.is_installed(MODEL, tmp_path)
    (folder / models.MARKER).write_text("not json", encoding="utf-8")
    assert not models.is_installed(MODEL, tmp_path)


def test_a_download_that_would_fill_the_disk_is_refused_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "_free_gb", lambda _folder: 12.0)
    hub = StandIn()
    big = Plan([RepoFile("a.safetensors", 3 * models.GB)], [])
    with pytest.raises(MiaError, match="not enough disk"):
        models.pull(MODEL, tmp_path, hub, big)
    assert hub.downloads == []


def test_what_is_already_there_counts_toward_the_disk_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = models.folder_for(MODEL, tmp_path)
    folder.mkdir(parents=True)
    (folder / "partial").write_bytes(b"x" * 1024)
    monkeypatch.setattr(models, "_free_gb", lambda _folder: 10.0)
    hub = StandIn(files=[RepoFile("a.safetensors", 1024)])
    models.pull(MODEL, tmp_path, hub, models.plan(MODEL, hub))
    assert hub.downloads == [["a.safetensors"]]


def test_the_command_asks_first_and_shows_the_license(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = StandIn()
    monkeypatch.setattr(models, "HuggingFace", lambda: hub)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "n")
    assert cli.main(["models", "pull"]) == 0
    out = capsys.readouterr().out
    assert "Apache 2.0 (open)" in out and "left out: .gitattributes, evil.py, old.ckpt" in out and "Nothing downloaded" in out
    assert hub.downloads == []
    assert cli.main(["models", "pull", "--yes"]) == 0
    assert hub.downloads
    assert "ready" in capsys.readouterr().out
    assert cli.main(["models", "pull", "--yes"]) == 0
    assert "already downloaded" in capsys.readouterr().out
    assert len(hub.downloads) == 1


def test_no_terminal_needs_yes(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    hub = StandIn()
    monkeypatch.setattr(models, "HuggingFace", lambda: hub)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["models", "pull"]) == 1
    assert "--yes" in capsys.readouterr().err
    assert hub.downloads == []


def test_the_list_shows_state_and_the_default(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["models"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(catalog.MODELS)
    assert lines[0].startswith(catalog.DEFAULT.id) and lines[0].endswith("not downloaded, default")


def test_the_catalog_is_pinned_open_and_has_the_tested_default() -> None:
    assert catalog.DEFAULT.id == "flux2-klein-4b-q4"
    assert len({model.id for model in catalog.MODELS}) == len(catalog.MODELS)
    for model in catalog.MODELS:
        assert len(model.revision) == 40 and all(c in "0123456789abcdef" for c in model.revision), model.id
    assert catalog.DEFAULT.license_class == catalog.OPEN
