"""The git hooks refuse what they promise to, run for real in a throwaway repository."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Split up, so a commit of this file doesn't trip the hook it tests.
SAMPLE_KEY = "-----BEGIN " + "PRIVATE KEY-----"
SAMPLE_TOKEN = "hf_" + "a" * 30


class Repo:
    def __init__(self, path: Path, home: Path) -> None:
        self.path = path
        self.env = {
            **os.environ,
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }

    def git(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.path, env={**self.env, **extra},
                              capture_output=True, text=True, check=False)  # fmt: skip

    def commit(self, name: str, text: str = "x\n", message: str = "a change", **extra: str) -> bool:
        file = self.path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        assert self.git("add", "-f", "--", name).returncode == 0
        return self.git("commit", "-q", "-m", message, **extra).returncode == 0


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    path = tmp_path / "repo"
    (path / "scripts").mkdir(parents=True)
    shutil.copytree(ROOT / ".githooks", path / ".githooks")
    shutil.copy(ROOT / "scripts" / "private-names.sh", path / "scripts" / "private-names.sh")
    made = Repo(path, tmp_path)
    assert made.git("init", "-q", "-b", "main").returncode == 0
    assert made.git("config", "core.hooksPath", ".githooks").returncode == 0
    return made


def on_branch(repo: Repo) -> Repo:
    assert repo.git("switch", "-q", "-c", "work").returncode == 0
    return repo


def test_a_plain_commit_on_a_branch_passes(repo: Repo) -> None:
    assert on_branch(repo).commit("src/thing.py")


def test_main_is_refused(repo: Repo) -> None:
    assert not repo.commit("src/thing.py")


@pytest.mark.parametrize(
    "name",
    [
        ".claude/settings.json",
        "sub/.claude/settings.json",
        ".mcp.json",
        "CLAUDE.local.md",
        "claude-session.txt",
        "plans/notes.md",
        "settings.toml",
        ".env",
        "local/mine.txt",
        "tls/server.key",
        "models/flux.safetensors",
        "flux.ckpt",
    ],
)
def test_own_files_claude_assets_keys_and_weights_are_refused(repo: Repo, name: str) -> None:
    assert not on_branch(repo).commit(name)


@pytest.mark.parametrize("name", ["CLAUDE.md", "PLAN.md", "settings.example.toml", ".env.example"])
def test_the_kept_files_pass(repo: Repo, name: str) -> None:
    assert on_branch(repo).commit(name)


@pytest.mark.parametrize("text", [SAMPLE_KEY, "HF_TOKEN=abc123", f"token = '{SAMPLE_TOKEN}'"])
def test_secrets_are_refused(repo: Repo, text: str) -> None:
    assert not on_branch(repo).commit("src/thing.py", text + "\n")


def test_an_image_needs_checking(repo: Repo) -> None:
    on_branch(repo)
    assert not repo.commit("images/logo.svg", "<svg/>\n")
    assert repo.git("commit", "-q", "-m", "an image", IMAGES_CHECKED="1").returncode == 0


def test_private_names_are_refused_in_files_and_messages(repo: Repo) -> None:
    on_branch(repo)
    (repo.path / ".git" / "hooks" / "private-names").write_text("secret-box\n", encoding="utf-8")
    assert not repo.commit("src/thing.py", "ssh secret_box\n")
    assert repo.commit("src/thing.py", "ssh my-mac.lan\n")
    assert not repo.commit("src/other.py", "x\n", message="fix SecretBox")
