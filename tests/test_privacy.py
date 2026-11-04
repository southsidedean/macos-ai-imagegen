"""Nothing in the repository describes a real machine or network: examples use stand-ins.

The stand-ins are values nobody's network uses for real: 192.168.1.0/24, the RFC 5737 ranges,
fd00::/48, my-mac.lan, and example.com addresses. On your own machine, the patterns in
.git/hooks/private-names (never committed) are checked too.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

ALLOWED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in ("192.168.1.0/24", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "fd00::/48")
]
# Private and shared address space. 100.64/10 is where Tailscale gives out addresses.
PRIVATE_NETWORKS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
]
ALLOWED_NAMES = re.compile(r"(^|\.)my-mac\.lan$", re.IGNORECASE)
ALLOWED_EMAIL = re.compile(
    r"^(noreply@anthropic\.com|[\w.+-]+@users\.noreply\.github\.com|[\w.+-]+@example\.(com|org|net))$",
    re.IGNORECASE,
)

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\d)")
IPV6_ULA = re.compile(r"(?<![\w:])f[cd][0-9a-f]{2}:[0-9a-f:]*[0-9a-f]", re.IGNORECASE)
LOCAL_NAME = re.compile(r"\b[\w.-]+\.(?:home\.arpa|ts\.net|internal|local|lan|home)\b(?!\.)", re.IGNORECASE)
# Ends in a top-level domain of letters, so "package@1.2.3" (a pinned version) isn't one.
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
HOME_PATH = re.compile(r"/(?:Users|home)/(?!ubuntu\b|runner\b|<)[\w.-]+")
# The plan and these notes stay free of what any one Mac has: its chip, memory, or free disk.
# A later user-facing page may state requirements, so this covers only these two files.
MACHINE_DETAILS = re.compile(
    r"\bM[1-9]\s+(?:Pro|Max|Ultra)\b|\bM[1-9]\b(?=\s+(?:chip|Mac))|\b\d+\s*[GT]B\s+(?:of\s+)?(?:memory|RAM|free)\b",
    re.IGNORECASE,
)

# The made-up names and addresses in these files are the point: they feed the checks on purpose.
SAMPLE_FILES = {"tests/test_privacy.py", "tests/test_hooks.py"}


def tracked_text_files() -> dict[str, str]:
    """Every tracked file that's text, by path, with its contents (the sample files aside)."""
    names = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    files = {}
    for name in filter(None, names.split(b"\0")):
        if name.decode() in SAMPLE_FILES:
            continue
        try:
            files[name.decode()] = (ROOT / name.decode()).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # images and other binary files
    return files


def private_addresses(text: str) -> list[str]:
    found = []
    for match in IPV4.findall(text) + IPV6_ULA.findall(text):
        try:
            address = ipaddress.ip_address(match)
        except ValueError:
            continue  # a version number that looks like an address, say
        if any(address in n for n in PRIVATE_NETWORKS) and not any(address in n for n in ALLOWED_NETWORKS):
            found.append(match)
    return found


def test_no_private_addresses() -> None:
    found = {path: hits for path, text in tracked_text_files().items() if (hits := private_addresses(text))}
    assert not found, f"use a stand-in such as 192.168.1.20 or 192.0.2.10 instead: {found}"


def test_no_local_host_names() -> None:
    found = {
        path: hits
        for path, text in tracked_text_files().items()
        if (hits := [n for n in LOCAL_NAME.findall(text) if not ALLOWED_NAMES.search(n)])
    }
    assert not found, f"use a stand-in such as my-mac.lan instead: {found}"


def test_no_personal_email_addresses() -> None:
    found = {
        path: hits
        for path, text in tracked_text_files().items()
        if (hits := [e for e in EMAIL.findall(text) if not ALLOWED_EMAIL.match(e)])
    }
    assert not found, f"use an example.com address instead: {found}"


def test_no_home_folders() -> None:
    found = {path: hits for path, text in tracked_text_files().items() if (hits := HOME_PATH.findall(text))}
    assert not found, f"use a placeholder such as ~/mia instead of a real home folder: {found}"


@pytest.mark.parametrize("name", ["PLAN.md", "CLAUDE.md"])
def test_no_machine_details_in_the_plan_or_notes(name: str) -> None:
    text = (ROOT / name).read_text(encoding="utf-8")
    assert not MACHINE_DETAILS.findall(text), f"{name} describes a real machine; say 'a test Mac' instead"


def test_nothing_from_the_private_names_list() -> None:
    # On a failure it names only the files, never the pattern, so the name isn't printed.
    result = subprocess.run(["sh", str(ROOT / "scripts" / "private-names.sh"), "patterns"], cwd=ROOT,
                            capture_output=True, text=True, check=False)  # fmt: skip
    if result.returncode == 3:
        pytest.skip("no private-names list here (CI, or a fresh clone)")
    assert result.returncode == 0, f"scripts/private-names.sh patterns failed: {result.stderr}"
    patterns = [re.compile(line, re.IGNORECASE) for line in result.stdout.splitlines() if line]
    found = sorted(path for path, text in tracked_text_files().items() if any(p.search(text) for p in patterns))
    assert not found, f"something from your private-names list is in these tracked files: {found}"


def test_the_checks_catch_what_they_should() -> None:
    # The patterns themselves, against made-up text, so a broken pattern can't pass silently.
    assert private_addresses("ssh 192.168.77.5 and 10.0.0.1 and 100.101.102.103")
    assert not private_addresses("example 192.168.1.20, docs 192.0.2.10, version 1.37.1")
    names = LOCAL_NAME.findall("nas.lan my-mac.lan x.ts.net")
    assert [n for n in names if not ALLOWED_NAMES.search(n)] == ["nas.lan", "x.ts.net"]
    assert HOME_PATH.findall("/Users/someone/projects and /home/ubuntu/mia") == ["/Users/someone"]
    assert EMAIL.findall("me@my-domain.com renovate@44.155.1") == ["me@my-domain.com"]
    for text in ("an M3 Max", "M4 Pro", "the M1 chip", "64 GB of memory", "16GB RAM", "80 GB free"):
        assert MACHINE_DETAILS.search(text), text
    for text in ("18 GB at 8-bit", "a 34 GB download", "M2 of the plan"):
        assert not MACHINE_DETAILS.search(text), text
