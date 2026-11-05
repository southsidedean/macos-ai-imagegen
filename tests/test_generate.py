"""Making an image, from the command down to the file, with the fake engine."""

from __future__ import annotations

import stat
import struct
from pathlib import Path

import pytest

from mia import catalog, cli, history, paths
from mia.engine import Cancelled, MiaError, Request
from mia.fake_engine import FakeEngine
from mia.generate import again, generate


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "mia"))
    monkeypatch.setenv("MIA_ENGINE", "fake")
    monkeypatch.delenv("MIA_MODELS", raising=False)


REQUEST = Request("a red fox", 42, 1024, 1024, 4)


def png_chunks(path: Path) -> list[str]:
    data, chunks, position = path.read_bytes(), [], 8
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        chunks.append(data[position + 4 : position + 8].decode())
        position += 12 + length
    return chunks


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"prompt": "   "}, "empty"),
        ({"prompt": "x" * 4001}, "too long"),
        ({"seed": -1}, "seed"),
        ({"seed": 2**32}, "seed"),
        ({"width": 1000}, "width"),
        ({"height": 128}, "height"),
        ({"height": 4096}, "height"),
        ({"steps": 0}, "steps"),
        ({"steps": 51}, "steps"),
    ],
)
def test_a_bad_request_is_refused_with_a_reason(change: dict[str, object], message: str) -> None:
    settings: dict[str, object] = {"prompt": "x", "seed": 1, "width": 512, "height": 512, "steps": 4, **change}
    with pytest.raises(MiaError, match=message):
        Request(**settings)  # type: ignore[arg-type]


def test_the_fake_engines_picture_follows_the_request(tmp_path: Path) -> None:
    def pixels(request: Request) -> str:
        return FakeEngine().generate(request, tmp_path / "x.png", lambda _s, _t: None, lambda: False)

    assert pixels(REQUEST) == pixels(REQUEST)
    assert pixels(REQUEST) != pixels(Request("a red fox", 43, 1024, 1024, 4))
    assert pixels(REQUEST) != pixels(Request("a grey fox", 42, 1024, 1024, 4))


def test_an_image_is_saved_recorded_and_private() -> None:
    record = generate(REQUEST, catalog.DEFAULT, FakeEngine())
    assert Path(record.file).parent == paths.images_folder()
    assert history.get(paths.history_file(), record.id) == record
    assert (record.repo, record.revision) == (catalog.DEFAULT.repo, catalog.DEFAULT.revision)
    assert stat.S_IMODE(paths.home_folder().stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.history_file().stat().st_mode) == 0o600


def test_a_folder_that_already_exists_is_made_private_too() -> None:
    # Downloading a model can make mia's folder first, with ordinary permissions.
    paths.home_folder().mkdir(parents=True)
    paths.home_folder().chmod(0o755)
    record = generate(REQUEST, catalog.DEFAULT, FakeEngine())
    assert stat.S_IMODE(paths.home_folder().stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(record.file).parent.stat().st_mode) == 0o700


def test_saved_images_hold_only_the_chunks_a_picture_needs() -> None:
    record = generate(REQUEST, catalog.DEFAULT, FakeEngine())
    assert png_chunks(Path(record.file)) == ["IHDR", "IDAT", "IEND"]
    assert b"red fox" not in Path(record.file).read_bytes()


def test_a_stopped_job_leaves_nothing_behind(tmp_path: Path) -> None:
    calls: list[int] = []
    output = tmp_path / "out.png"
    with pytest.raises(Cancelled, match="step 2 of 4"):
        generate(REQUEST, catalog.DEFAULT, FakeEngine(), output, lambda step, _t: calls.append(step), lambda: len(calls) == 2)
    assert calls == [1, 2]
    assert not output.exists() and not list(tmp_path.glob(".*partial*"))
    assert history.recent(paths.history_file()) == []


def test_a_crash_in_the_engine_leaves_nothing_behind(tmp_path: Path) -> None:
    class Broken(FakeEngine):
        def generate(self, request: Request, output: Path, progress: object, cancelled: object) -> str:
            output.write_bytes(b"half a pic")
            raise OSError("out of memory")

    with pytest.raises(OSError, match="out of memory"):
        generate(REQUEST, catalog.DEFAULT, Broken(), tmp_path / "out.png")
    assert not (tmp_path / "out.png").exists()
    assert not list(tmp_path.glob(".*partial*"))


def test_output_must_be_a_png(tmp_path: Path) -> None:
    with pytest.raises(MiaError, match=r"\.png"):
        generate(REQUEST, catalog.DEFAULT, FakeEngine(), tmp_path / "out.jpg")


def test_the_command_makes_an_image_and_remakes_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["generate", "a red fox", "--seed", "42", "--width", "512", "--height", "512"]) == 0
    captured = capsys.readouterr()
    first = Path(captured.out.strip())
    assert first.exists()
    assert "step 4 of 4, then finishing up" in captured.err
    assert "a red fox" not in captured.err  # prompts stay out of what might get logged
    image_id = first.stem

    assert cli.main(["generate", "--again", image_id]) == 0
    captured = capsys.readouterr()
    assert f"identical pixels to {image_id}: yes" in captured.err
    assert Path(captured.out.strip()) != first
    assert len(history.recent(paths.history_file())) == 2


def test_the_command_says_when_a_remake_differs(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.main(["generate", "a red fox", "--seed", "42"]) == 0
    image_id = Path(capsys.readouterr().out.strip()).stem

    class Drifted(FakeEngine):
        version = "2"

        def generate(self, request: Request, output: Path, progress: object, cancelled: object) -> str:
            output.write_bytes(b"x")
            return "different"

    monkeypatch.setattr(cli, "load_engine", lambda _model: Drifted())
    assert cli.main(["generate", "--again", image_id]) == 1
    err = capsys.readouterr().err
    assert "identical pixels" in err and ": no" in err and "not 1" in err


def test_remaking_refuses_a_changed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    record = generate(REQUEST, catalog.DEFAULT, FakeEngine())
    moved = catalog.Model(**{**catalog.DEFAULT.__dict__, "revision": "new"})
    monkeypatch.setattr(catalog, "MODELS", (moved, *catalog.MODELS[1:]))
    with pytest.raises(MiaError, match="different revision"):
        again(record.id)


def test_problems_are_one_line_and_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    for arguments, expected in (
        (["generate"], "say what to make"),
        (["generate", "x", "--model", "nope"], "no model called"),
        (["generate", "x", "--width", "100"], "width"),
        (["generate", "--again", "nope"], "no image"),
    ):
        assert cli.main(arguments) == 1
        err = capsys.readouterr().err
        assert expected in err and "Traceback" not in err


def test_stopping_exits_130_and_says_so(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "generate", interrupted)
    assert cli.main(["generate", "x"]) == cli.EXIT_STOPPED
    assert "no image was kept" in capsys.readouterr().err


def test_a_real_model_must_be_downloaded_first(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIA_ENGINE")
    assert cli.main(["generate", "x"]) == 1
    assert f"mia models pull {catalog.DEFAULT.id}" in capsys.readouterr().err


def test_history_lists_newest_first(capsys: pytest.CaptureFixture[str]) -> None:
    older = generate(Request("first", 1, 512, 512, 1), catalog.DEFAULT, FakeEngine())
    newer = generate(Request("second", 2, 512, 512, 1), catalog.DEFAULT, FakeEngine())
    assert [r.id for r in history.recent(paths.history_file())] == [newer.id, older.id]
    assert cli.main(["history"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(newer.id) and lines[0].endswith("second")


def test_a_history_from_a_newer_mia_is_refused() -> None:
    generate(REQUEST, catalog.DEFAULT, FakeEngine())
    import sqlite3

    connection = sqlite3.connect(paths.history_file())
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    with pytest.raises(OSError, match="newer version"):
        history.recent(paths.history_file())
