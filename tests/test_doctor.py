"""mia doctor judges any machine correctly, and changes nothing on this one."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mia import __version__, cli, doctor, paths

READY = doctor.Facts(
    system="Darwin",
    machine="arm64",
    python=(3, 13),
    memory_gb=32.0,
    folder=Path("/nowhere/mia"),
    disk_free_gb=100.0,
    engines={"mflux": True, "mlx": True},
    models=2,
)


def status(facts: doctor.Facts, name: str) -> str:
    return next(f.status for f in doctor.findings(facts) if f.name == name)


def test_a_ready_machine_has_no_problems() -> None:
    assert not [f for f in doctor.findings(READY) if f.status in (doctor.WARN, doctor.FAIL)]


@pytest.mark.parametrize(
    ("system", "machine"), [("Linux", "x86_64"), ("Linux", "aarch64"), ("Darwin", "x86_64")]
)
def test_only_an_apple_silicon_mac_passes(system: str, machine: str) -> None:
    assert status(replace(READY, system=system, machine=machine), "Apple Silicon Mac") == doctor.FAIL


@pytest.mark.parametrize(
    ("python", "expected"),
    [((3, 12), doctor.PASS), ((3, 13), doctor.PASS), ((3, 14), doctor.PASS), ((3, 15), doctor.WARN), ((3, 11), doctor.WARN)],
)
def test_python_versions_follow_the_engine(python: tuple[int, int], expected: str) -> None:
    assert status(replace(READY, python=python), "Python") == expected


@pytest.mark.parametrize(
    ("free", "expected"),
    [(5.9, doctor.FAIL), (6.0, doctor.WARN), (29.9, doctor.WARN), (30.0, doctor.PASS), (None, doctor.WARN)],
)
def test_disk_limits(free: float | None, expected: str) -> None:
    assert status(replace(READY, disk_free_gb=free), "Disk") == expected


@pytest.mark.parametrize(
    ("memory", "expected"), [(8.0, doctor.WARN), (15.9, doctor.WARN), (16.0, doctor.PASS), (None, doctor.WARN)]
)
def test_memory_only_warns(memory: float | None, expected: str) -> None:
    assert status(replace(READY, memory_gb=memory), "Memory") == expected


def test_a_missing_engine_is_not_a_problem_yet() -> None:
    assert status(replace(READY, engines={"mflux": False, "mlx": False}), "Image engine") == doctor.INFO


def test_mia_home_moves_the_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "elsewhere"))
    assert paths.home_folder() == tmp_path / "elsewhere"
    assert doctor.gather().folder == tmp_path / "elsewhere"


def test_models_are_counted_and_nothing_is_made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "mia"
    monkeypatch.setenv("MIA_HOME", str(home))
    facts = doctor.gather()
    assert facts.models == 0
    assert facts.disk_free_gb is not None  # read from the nearest folder that exists
    assert not home.exists()
    (home / "models" / "one").mkdir(parents=True)
    (home / "models" / "stray-file").write_text("x", encoding="utf-8")
    assert doctor.gather().models == 1


def test_the_exit_code_means_a_real_problem(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor, "gather", lambda: READY)
    assert cli.main(["doctor"]) == 0
    assert "PASS  Disk: 100 GB free" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "gather", lambda: replace(READY, disk_free_gb=1.0))
    assert cli.main(["doctor"]) == 1
    monkeypatch.setattr(doctor, "gather", lambda: replace(READY, python=(3, 15), memory_gb=8.0))
    assert cli.main(["doctor"]) == 0  # warnings alone don't fail it


def test_the_command_needs_a_subcommand_and_knows_its_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main([])
    assert stopped.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert __version__ in capsys.readouterr().out
