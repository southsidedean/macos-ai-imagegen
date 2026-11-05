"""The queue and the worker, run one step at a time with the fake engine."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mia import catalog, db, gpu, history, jobs, paths, tokens
from mia.engine import Cancelled, Engine, MiaError, Request
from mia.fake_engine import FakeEngine
from mia.generate import generate
from mia.supervisor import STALL_SECONDS, STALLED, Supervisor, recover_if_free
from mia.worker import NewModelNeeded, Worker, sweep_images

KLEIN, SCHNELL = catalog.MODELS[0].id, catalog.MODELS[1].id
REQUEST = Request("a red fox", 42, 512, 512, 2)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "mia"))
    monkeypatch.setenv("MIA_ENGINE", "fake")


def worker() -> Worker:
    return Worker(lambda _model: FakeEngine())


def state(job_id: str) -> jobs.Job:
    found = jobs.get(job_id)
    assert found is not None
    return found


def test_a_job_runs_and_records_its_image() -> None:
    job, new = jobs.submit(REQUEST, KLEIN)
    assert new and job.state == jobs.QUEUED
    assert worker().step() is True
    done = state(job.id)
    assert done.state == jobs.DONE and done.step == 2 and done.started and done.finished
    assert done.image is not None
    record = history.get(paths.history_file(), done.image)
    assert record is not None and record.prompt == "a red fox" and Path(record.file).exists()


def test_jobs_run_in_order_and_an_empty_queue_says_so() -> None:
    first, _ = jobs.submit(REQUEST, KLEIN)
    second, _ = jobs.submit(REQUEST, KLEIN)
    running = worker()
    running.step()
    assert state(first.id).state == jobs.DONE and state(second.id).state == jobs.QUEUED
    running.step()
    assert state(second.id).state == jobs.DONE
    assert running.step() is False


def test_the_queue_has_a_limit() -> None:
    for _ in range(jobs.MAX_QUEUED):
        jobs.submit(REQUEST, KLEIN)
    with pytest.raises(jobs.QueueFull):
        jobs.submit(REQUEST, KLEIN)


def test_an_idempotency_key_returns_the_first_job() -> None:
    first, new = jobs.submit(REQUEST, KLEIN, "device", "key")
    again, again_new = jobs.submit(REQUEST, KLEIN, "device", "key")
    assert new and not again_new and first.id == again.id
    other_device, is_new = jobs.submit(REQUEST, KLEIN, "other", "key")
    assert is_new and other_device.id != first.id
    with pytest.raises(jobs.Conflict, match="different request"):
        jobs.submit(Request("a blue fox", 42, 512, 512, 2), KLEIN, "device", "key")


def test_cancelling_a_waiting_job_means_it_never_runs() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    cancelled = jobs.cancel(job.id)
    assert cancelled is not None and cancelled.state == jobs.CANCELLED
    assert worker().step() is False
    assert state(job.id).image is None


def test_cancelling_a_running_job_stops_it_at_the_next_step() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)

    class CancelledPartway(FakeEngine):
        def generate(self, request: Request, output: Path, progress: object, cancelled: object) -> str:
            jobs.cancel(job.id)
            return super().generate(request, output, progress, cancelled)  # type: ignore[arg-type]

    Worker(lambda _model: CancelledPartway()).step()
    stopped = state(job.id)
    assert stopped.state == jobs.CANCELLED and stopped.step == 1 and stopped.image is None
    assert history.recent(paths.history_file()) == []
    assert not list(paths.images_folder().glob("*"))


def test_a_finished_job_cant_be_cancelled_and_a_missing_one_says_so() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    worker().step()
    with pytest.raises(jobs.Conflict, match="already done"):
        jobs.cancel(job.id)
    assert jobs.cancel("nothing") is None


def test_an_engine_crash_fails_the_job_without_quoting_it(capsys: pytest.CaptureFixture[str]) -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)

    class Crashing(FakeEngine):
        def generate(self, request: Request, output: Path, progress: object, cancelled: object) -> str:
            raise ValueError(f"could not handle {request.prompt}")

    running = Worker(lambda _model: Crashing())
    assert running.step() is True
    failed = state(job.id)
    assert failed.state == jobs.FAILED
    assert failed.error is not None and "ValueError" in failed.error and "red fox" not in failed.error
    assert "red fox" not in capsys.readouterr().err  # prompts stay out of the worker's log


def test_a_model_that_wont_load_fails_only_its_own_job() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)

    def broken(_model: catalog.Model) -> Engine:
        raise MiaError("it isn't downloaded yet")

    Worker(broken).step()
    assert state(job.id).state == jobs.FAILED and state(job.id).error == "it isn't downloaded yet"


def test_another_model_means_a_new_worker_and_the_job_waits_for_it() -> None:
    first, _ = jobs.submit(REQUEST, KLEIN)
    second, _ = jobs.submit(REQUEST, SCHNELL)
    running = worker()
    running.step()
    assert state(first.id).state == jobs.DONE
    with pytest.raises(NewModelNeeded):
        running.step()
    assert state(second.id).state == jobs.QUEUED
    assert worker().step() is True
    assert state(second.id).state == jobs.DONE


def test_an_abandoned_job_is_failed_with_the_reason_and_not_retried() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id)
    assert jobs.recover_abandoned() == [job.id]
    failed = state(job.id)
    assert failed.state == jobs.FAILED and failed.error is not None and "worker stopped" in failed.error
    assert worker().step() is False


def test_an_abandoned_job_being_cancelled_counts_as_cancelled() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id)
    jobs.cancel(job.id)
    jobs.recover_abandoned()
    assert state(job.id).state == jobs.CANCELLED and state(job.id).error is None


def test_a_cancel_ignored_for_too_long_is_noticed() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id)
    assert not jobs.stuck_in_cancel(0)
    jobs.cancel(job.id)
    assert jobs.stuck_in_cancel(-1)
    assert not jobs.stuck_in_cancel(3600)


def test_the_gpu_can_be_held_by_only_one() -> None:
    with gpu.hold(), pytest.raises(MiaError, match="using the GPU"), gpu.hold():
        pass
    with gpu.hold():  # free again
        pass


def test_only_a_real_holder_is_named_in_the_lock_file() -> None:
    # Tests and scripts read this to find the worker to kill. Naming the server would get the server killed.
    with gpu.hold():
        pass
    lock = paths.home_folder() / "gpu.lock"
    named = lock.read_text()
    with gpu.hold(briefly=True):
        assert lock.read_text() == named


def test_waiting_for_the_gpu_can_be_given_up() -> None:
    answers = iter([True, False])
    with gpu.hold(), pytest.raises(gpu.Busy), gpu.hold(wait_while=lambda: next(answers)):
        pass


def test_the_command_wont_share_the_gpu_with_a_worker(capsys: pytest.CaptureFixture[str]) -> None:
    from mia import cli

    with gpu.hold():
        assert cli.main(["generate", "a red fox"]) == 1
    assert "using the GPU" in capsys.readouterr().err


def test_a_database_from_0_2_moves_forward_and_keeps_its_images() -> None:
    # The file a 0.2 install has: version 1, with only the images table.
    path = paths.history_file()
    path.parent.mkdir(parents=True)
    old = sqlite3.connect(path)
    old.execute(db.MIGRATIONS[0][0])
    old.execute(
        "INSERT INTO images VALUES ('abc123abc123', 'now', 'an old fox', 1, 512, 512, 4, 'm', 'r', 'v', 'e', 'ev', 'f', 'p')"
    )
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    assert history.get(path, "abc123abc123") is not None
    job, _ = jobs.submit(REQUEST, KLEIN)
    assert jobs.get(job.id) is not None
    connection = db.connect()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA
    connection.close()


def test_an_interrupt_the_engine_swallows_still_stops_the_worker() -> None:
    # mflux's step loop catches KeyboardInterrupt and ends the job, so the worker sees only a cancelled job nobody
    # asked to cancel. It has to stop, and not go on to take the next one.
    first, _ = jobs.submit(REQUEST, KLEIN)
    second, _ = jobs.submit(REQUEST, KLEIN)

    class Interrupted(FakeEngine):
        def generate(self, request: Request, output: Path, progress: object, cancelled: object) -> str:
            raise Cancelled("interrupted")

    running = Worker(lambda _model: Interrupted())
    assert running.step() is True
    assert running.stopping
    assert state(first.id).state == jobs.FAILED and "stopped" in (state(first.id).error or "")
    assert state(second.id).state == jobs.QUEUED


def test_a_failure_recording_the_image_leaves_nothing_half_done() -> None:
    # The record and the job's finish are one transaction.
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id)

    def broken(_connection: sqlite3.Connection, _record: history.Record) -> None:
        raise OSError("the disk went away")

    with pytest.raises(OSError, match="went away"):
        generate(REQUEST, catalog.DEFAULT, FakeEngine(), then=broken)
    assert history.recent(paths.history_file()) == []
    assert state(job.id).state == jobs.RUNNING


def test_a_picture_a_killed_worker_never_recorded_is_removed_and_nothing_else() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id, "cccccccccccc")
    jobs.recover_abandoned()
    kept = Path(generate(REQUEST, catalog.DEFAULT, FakeEngine()).file)
    folder = kept.parent
    orphan = folder / "cccccccccccc.png"
    partial = folder / ".bbbbbbbbbbbb.partial.png"
    yours = folder / "my-holiday.png"
    for path in (orphan, partial, yours):
        path.write_bytes(b"x")
    sweep_images()
    assert kept.exists() and yours.exists()
    assert not orphan.exists() and not partial.exists()
    assert jobs.plans_left_behind() == []  # done with, and never looked at again


def test_a_lost_history_never_costs_you_your_pictures() -> None:
    # A sweep that looked for pictures with no record would delete every one of these.
    record = generate(REQUEST, catalog.DEFAULT, FakeEngine())
    picture = Path(record.file)
    for path in paths.home_folder().glob("history.sqlite3*"):
        path.unlink()
    sweep_images()
    assert picture.exists()


def test_a_picture_saved_under_a_name_like_ours_is_safe(tmp_path: Path) -> None:
    paths.make_private(paths.home_folder())
    paths.make_private(paths.images_folder())
    named = paths.images_folder() / "deadbeef0001.png"
    generate(REQUEST, catalog.DEFAULT, FakeEngine(), named)
    sweep_images()
    assert named.exists()


def test_a_recorded_picture_is_kept_even_if_its_job_failed() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id, "dddddddddddd")
    record = generate(REQUEST, catalog.DEFAULT, FakeEngine(), image_id="dddddddddddd")
    jobs.recover_abandoned()
    sweep_images()
    assert Path(record.file).exists()


def test_a_file_that_cant_be_removed_doesnt_stop_the_queue() -> None:
    paths.make_private(paths.home_folder())
    paths.make_private(paths.images_folder())
    (paths.images_folder() / ".aaaaaaaaaaaa.partial.png").mkdir()
    sweep_images()  # no error
    job, _ = jobs.submit(REQUEST, KLEIN)
    worker().step()
    assert state(job.id).state == jobs.DONE


def test_a_job_records_which_image_it_planned_and_forgets_on_success() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    worker().step()
    done = state(job.id)
    assert done.planned_image is None and done.image is not None
    assert jobs.plans_left_behind() == []


def test_old_finished_jobs_are_forgotten_and_others_arent() -> None:
    old, _ = jobs.submit(REQUEST, KLEIN)
    jobs.cancel(old.id)
    waiting, _ = jobs.submit(REQUEST, KLEIN)
    jobs.prune(days=30)
    assert jobs.get(old.id) is not None
    jobs.prune(days=-1)  # everything finished is now older than the cutoff
    assert jobs.get(old.id) is None
    assert jobs.get(waiting.id) is not None


def test_a_retry_racing_its_first_attempt_gets_the_same_job() -> None:
    # Each request with no seed picks its own, so without seed_given these looked like different requests.
    first, _ = jobs.submit(Request("a fox", 1, 512, 512, 2), KLEIN, "device", "key", seed_given=False)
    again, new = jobs.submit(Request("a fox", 2, 512, 512, 2), KLEIN, "device", "key", seed_given=False)
    assert not new and again.id == first.id and again.seed == 1


def test_recovery_can_only_finish_a_job_still_running() -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    assert jobs.recover_abandoned() == []
    assert state(job.id).state == jobs.QUEUED


def test_the_supervisor_doesnt_touch_a_job_while_another_process_holds_the_gpu() -> None:
    # A second server, or an orphaned worker, must not fail a job a live worker is running.
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id)
    with gpu.hold():
        recover_if_free()
    assert state(job.id).state == jobs.RUNNING
    recover_if_free()
    assert state(job.id).state == jobs.FAILED


def test_a_closed_stderr_doesnt_stop_the_supervisor_or_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from mia import supervisor, worker as worker_module

    class Closed:
        def write(self, _text: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    monkeypatch.setattr("sys.stderr", Closed())
    supervisor._log("x")
    worker_module._log("x")


def test_the_supervisor_survives_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    # It used to die for good on a locked database, with the API still looking healthy.
    calls: list[int] = []

    def flaky(_self: Supervisor) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Supervisor, "_check", flaky)
    monkeypatch.setattr("mia.supervisor.CHECK_SECONDS", 0.01)
    supervisor = Supervisor()
    supervisor.start()
    deadline = time.monotonic() + 5
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) >= 3
    supervisor.stop()


def test_a_dead_workers_job_is_retried_if_recovery_fails_the_first_time(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = Supervisor()

    class Dead:
        def poll(self) -> int:
            return -9

    supervisor._process = Dead()  # type: ignore[assignment]
    outcomes = iter([sqlite3.OperationalError("database is locked"), None])

    def recover(_reason: str | None = None) -> None:
        outcome = next(outcomes)
        if outcome:
            raise outcome

    monkeypatch.setattr("mia.supervisor.recover_if_free", recover)
    with pytest.raises(sqlite3.OperationalError):
        supervisor._check()
    assert supervisor._process is not None  # not forgotten, so the next check tries again
    supervisor._check()
    assert supervisor._process is None


def test_a_token_is_shown_once_stored_as_a_hash_and_can_be_revoked() -> None:
    issued = tokens.create("laptop", tokens.GENERATE)
    assert issued.startswith("mia_") and len(issued) > 40
    device = tokens.verify(issued)
    assert device is not None and device.name == "laptop"
    assert device.allows(tokens.READ) and device.allows(tokens.GENERATE) and not device.allows(tokens.ADMIN)
    assert issued.encode() not in paths.history_file().read_bytes()
    connection = db.connect()
    rows = connection.execute("SELECT digest FROM tokens").fetchall()
    connection.close()
    assert rows and issued not in rows[0][0]
    assert tokens.verify("mia_wrong") is None and tokens.verify("") is None and tokens.verify(issued + "x") is None
    tokens.revoke("laptop")
    assert tokens.verify(issued) is None
    with pytest.raises(MiaError, match="no active token"):
        tokens.revoke("laptop")


def test_the_token_commands(capsys: pytest.CaptureFixture[str]) -> None:
    from mia import cli

    assert cli.main(["token", "create", "--for", "laptop", "--scope", "read"]) == 0
    captured = capsys.readouterr()
    issued = captured.out.strip()
    assert tokens.verify(issued) is not None
    assert "won't be shown again" in captured.err and issued not in captured.err
    assert cli.main(["token", "create", "--for", "phone"]) == 0
    capsys.readouterr()

    assert cli.main(["token", "list"]) == 0
    listed = capsys.readouterr().out
    assert "phone  generate  never used" in listed
    assert "laptop  read  used" in listed and issued not in listed  # verifying it just now counts as use

    assert cli.main(["token", "revoke", "laptop"]) == 0
    assert "Revoked" in capsys.readouterr().out
    assert tokens.verify(issued) is None
    assert cli.main(["token"]) == 0  # no action lists them too
    assert "revoked" in capsys.readouterr().out
    assert cli.main(["token", "revoke", "laptop"]) == 1
    assert "no active token" in capsys.readouterr().err


def test_token_names_and_scopes_are_checked() -> None:
    tokens.create("phone")
    with pytest.raises(MiaError, match="already a token"):
        tokens.create("phone")
    for bad in ("", "has space", "../up", "x" * 65, "new\nline"):
        with pytest.raises(MiaError, match="name"):
            tokens.create(bad)
    with pytest.raises(MiaError, match="scope"):
        tokens.create("tablet", "root")
    tokens.revoke("phone")
    assert tokens.create("phone")  # the name is free again


class _Running:
    """A worker that's alive until killed, for checks that only look at what the supervisor decides."""

    killed = False

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True


def _watching(monkeypatch: pytest.MonkeyPatch) -> tuple[Supervisor, _Running, list[float]]:
    clock = [1000.0]
    monkeypatch.setattr("mia.supervisor.time.monotonic", lambda: clock[0])
    supervisor = Supervisor()
    process = _Running()
    supervisor._process = process  # type: ignore[assignment]
    return supervisor, process, clock


def test_a_job_that_stops_progressing_is_failed_and_the_next_can_run(monkeypatch: pytest.MonkeyPatch) -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    waiting, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id)
    supervisor, process, clock = _watching(monkeypatch)
    supervisor._check()  # first sight of it
    clock[0] += STALL_SECONDS
    supervisor._check()
    assert not process.killed  # not yet: only past the limit
    clock[0] += 1
    supervisor._check()
    assert process.killed
    supervisor._check()  # finds it dead
    ended = state(job.id)
    assert ended.state == jobs.FAILED and ended.error == STALLED
    assert state(waiting.id).state == jobs.QUEUED
    assert supervisor._process is None and supervisor._ended_because is None


def test_a_slow_job_that_keeps_progressing_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id)
    supervisor, process, clock = _watching(monkeypatch)
    supervisor._check()
    for step in range(1, 5):  # each step takes most of the limit, so the whole job takes far longer than it
        clock[0] += STALL_SECONDS - 1
        jobs.set_step(job.id, step)
        supervisor._check()
    assert not process.killed


def test_an_idle_worker_is_never_stalled(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, process, clock = _watching(monkeypatch)
    supervisor._check()
    clock[0] += STALL_SECONDS * 10
    supervisor._check()
    assert not process.killed


def test_the_next_job_gets_its_own_time(monkeypatch: pytest.MonkeyPatch) -> None:
    first, _ = jobs.submit(REQUEST, KLEIN)
    second, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(first.id)
    supervisor, process, clock = _watching(monkeypatch)
    supervisor._check()
    clock[0] += STALL_SECONDS - 1
    jobs.fail(first.id, "over")
    jobs.claim(second.id)
    supervisor._check()
    clock[0] += STALL_SECONDS - 1
    supervisor._check()
    assert not process.killed


def test_a_cancelled_job_that_stalls_still_counts_as_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    job, _ = jobs.submit(REQUEST, KLEIN)
    jobs.claim(job.id)
    jobs.cancel(job.id)
    supervisor, process, _clock = _watching(monkeypatch)
    process.killed = True
    supervisor._check()
    assert state(job.id).state == jobs.CANCELLED


def test_a_hung_engine_is_killed_for_real(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real worker, stuck in a step far longer than the test, which only a kill can end.
    monkeypatch.setenv("MIA_FAKE_STEP_SECONDS", "300")
    monkeypatch.setattr("mia.supervisor.STALL_SECONDS", 2.0)
    monkeypatch.setattr("mia.supervisor.CHECK_SECONDS", 0.1)
    job, _ = jobs.submit(REQUEST, KLEIN)
    supervisor = Supervisor()
    supervisor.start()
    try:
        deadline = time.monotonic() + 30
        while state(job.id).state != jobs.FAILED and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        supervisor.stop()
    assert state(job.id).error == STALLED
