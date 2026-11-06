"""The rules about what the repository holds, and how its workflows are written."""

from __future__ import annotations

import re
import struct
import subprocess
from pathlib import Path

import pytest

import mia

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_CLAUDE_ASSETS = {"CLAUDE.md", "PLAN.md"}
# What counts as a Claude asset, beyond the two that are kept. Case is ignored.
CLAUDE_ASSET = re.compile(r"(^|/)(\.claude(/|$)|\.mcp\.json$|claude[^/]*$|plans/)", re.IGNORECASE)
WEIGHTS = re.compile(r"\.(safetensors|ckpt|gguf|pt|pth)$", re.IGNORECASE)
PINNED = re.compile(r"^\s*(?:-\s+)?uses:\s+[\w./-]+@[0-9a-f]{40}\s+#\s+v\d")


def tracked() -> list[str]:
    output = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [name.decode() for name in output.split(b"\0") if name]


def test_only_the_two_claude_assets_are_tracked() -> None:
    found = [p for p in tracked() if p not in ALLOWED_CLAUDE_ASSETS and CLAUDE_ASSET.search(p)]
    assert not found, f"Claude assets other than CLAUDE.md and PLAN.md are tracked: {found}"


@pytest.mark.parametrize(
    "path",
    [".claude/settings.json", ".mcp.json", "CLAUDE.local.md", "claude-session.txt", "plans/notes.md"],
)
def test_claude_assets_are_ignored(path: str) -> None:
    result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, check=False)
    assert result.returncode == 0, f"{path} isn't in .gitignore"


@pytest.mark.parametrize("path", sorted(ALLOWED_CLAUDE_ASSETS))
def test_the_kept_assets_arent_ignored(path: str) -> None:
    result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, check=False)
    assert result.returncode == 1, f"{path} must stay committed"


def test_the_asset_pattern_catches_what_it_should() -> None:
    for path in (".claude/x", "sub/.claude/x", ".mcp.json", "CLAUDE.local.md", "claude-session.txt", "plans/a.md"):
        assert CLAUDE_ASSET.search(path), path
    assert not CLAUDE_ASSET.search("src/mia/engine.py")


def test_no_model_weights_are_tracked() -> None:
    assert not [p for p in tracked() if WEIGHTS.search(p)]


def test_every_action_is_pinned_to_a_commit() -> None:
    # A tag can be moved to other code later; a full commit hash can't. The comment says which
    # release it is, for people and for Renovate.
    loose = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*(?:-\s+)?uses:", line) and not PINNED.match(line):
                loose.append(f"{workflow.name}:{number}")
    assert not loose, f"pin these to a full commit with the version in a comment: {loose}"


def test_the_social_preview_is_githubs_size_with_no_metadata() -> None:
    data = (ROOT / "images" / "social-preview.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1280, 640)
    chunks, position = set(), 8
    while position < len(data):
        length = int.from_bytes(data[position : position + 4])
        chunks.add(data[position + 4 : position + 8].decode("latin1"))
        position += 12 + length
    assert not chunks & {"tEXt", "zTXt", "iTXt", "eXIf", "tIME"}, f"strip these: {sorted(chunks)}"


@pytest.mark.parametrize("name", ["mia-logo.svg", "mia-logo-dark.svg"])
def test_the_logos_are_labelled_and_self_contained(name: str) -> None:
    logo = (ROOT / "images" / name).read_text(encoding="utf-8")
    assert 'aria-label="mia logo' in logo
    assert "<script" not in logo
    assert "href" not in logo


@pytest.mark.parametrize("name", ["README.md", "CHANGELOG.md", "SECURITY.md", "PLAN.md", "docs/roadmap.md", "docs/api.md", "docs/mcp.md", "docs/remote.md", "docs/service.md", "docs/web.md"])
def test_links_between_our_own_files_work(name: str) -> None:
    source = ROOT / name
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)\s]*)?\)|srcset=\"([^\"]+)\"|src=\"([^\"]+)\"", source.read_text(encoding="utf-8")):
        link = next(part for part in target if part)
        if not re.match(r"[a-z]+:", link):
            assert (source.parent / link).exists(), f"{name} links to {link}, which isn't there"


def test_the_roadmap_and_changelog_name_this_version() -> None:
    version = mia.__version__.removesuffix(".dev0")
    assert version in (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    assert version in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def release_notes(version: str, changelog: Path) -> subprocess.CompletedProcess[str]:
    script = ROOT / "scripts" / "release-notes.sh"
    return subprocess.run(["sh", str(script), version, str(changelog)], capture_output=True, text=True, check=False)


def test_release_notes_are_one_finished_section(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## 0.3.0 (unreleased)\n\n- new\n\n## 0.2.0 (2026-11-04)\n\n- first\n\n## 0.1.0 (2026-01-01)\n\n"
        "## 0.0.1 (2025-01-01)\n\n- old\n",
        encoding="utf-8",
    )
    done = release_notes("0.2.0", changelog)
    assert done.returncode == 0
    assert done.stdout.strip() == "- first"
    for version in ("0.3.0", "0.1.0", "9.9.9", "0.2"):  # unreleased, empty, missing, and a partial match
        assert release_notes(version, changelog).returncode == 1, version


def test_the_package_has_a_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+)?", mia.__version__)
