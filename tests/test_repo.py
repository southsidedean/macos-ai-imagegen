"""The rules about what the repository holds, and how its workflows are written."""

from __future__ import annotations

import re
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


def test_the_package_has_a_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+)?", mia.__version__)
