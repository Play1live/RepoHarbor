import errno
import json
import os
import signal
import sys
import threading
import time
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from repoharbor import cli, integrity, status
from repoharbor.control import request
from repoharbor.credentials import Credentials
from repoharbor.errors import GitError, HarborError, Result
from repoharbor.git_backup import Git
from repoharbor.persistence import integrity_state, utcnow
from repoharbor.scheduler import Cron, Scheduler


def eventually(predicate):
    deadline = time.monotonic() + 6
    while not predicate():
        assert time.monotonic() < deadline
        threading.Event().wait(0.025)


def test_service_integrity_and_backup_overlap_without_queue(factory, monkeypatch):
    with factory() as engine:
        engine.config = replace(
            engine.config,
            schedule="2,5 * * * *",
            integrity_check=True,
            integrity=replace(engine.config.integrity, schedule="1-5 * * * *"),
            manual_run=True,
        )
        clock = Mock()
        clock.now.return_value = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
        monkeypatch.setattr(cli, "datetime", clock)

        def minute(number):
            clock.now.return_value = datetime(2026, 1, 1, 0, number, tzinfo=UTC)

        scheduled, manual_started, checking = (threading.Event() for _ in range(3))
        releases = [threading.Event() for _ in range(3)]

        def backup(*, manual=False, **kwargs):
            index = int(manual)
            (scheduled, manual_started)[index].set()
            assert releases[index].wait(10)
            return Result.SUCCESS

        monkeypatch.setattr(engine, "run", Mock(side_effect=backup))

        def check(engine):
            checking.set()
            assert releases[2].wait(10)

        mock_check = Mock(side_effect=check)
        monkeypatch.setattr(integrity, "run", mock_check)
        failures = []

        def serve():
            try:
                cli.serve(engine, engine.git.stop)
            except Exception as error:
                failures.append(error)

        service = threading.Thread(target=serve)
        result = []
        manual_thread = threading.Thread(
            target=lambda: result.append(request(engine.config.root, b"all"))
        )

        def live():
            return request(engine.config.root, b"status")

        service.start()
        try:
            eventually(lambda: live() == Result.SUCCESS)
            mock_check.assert_not_called()
            minute(2)
            assert scheduled.wait(5)
            eventually(
                lambda: engine.store.data.get("integrity", {}).get("skipped_overlaps")
                == 1
            )
            releases[0].set()
            eventually(lambda: live() == Result.SUCCESS)
            manual_thread.start()
            assert manual_started.wait(5)
            minute(3)
            eventually(lambda: engine.store.data["integrity"]["skipped_overlaps"] == 2)
            releases[1].set()
            manual_thread.join(5)
            assert result == [Result.SUCCESS]
            eventually(lambda: live() == Result.SUCCESS)
            mock_check.assert_not_called()

            minute(4)
            assert checking.wait(5)
            assert request(engine.config.root, b"all") == Result.BUSY
            minute(5)
            eventually(lambda: engine.store.data["integrity"]["skipped_overlaps"] == 3)
            assert engine.store.data["scheduler"]["skipped_overlaps"] == 1
            releases[2].set()
            eventually(lambda: live() == Result.SUCCESS)
            before = clock.now.call_count
            minute(9)
            eventually(lambda: clock.now.call_count >= before + 3)
            assert engine.run.call_count == 2
            mock_check.assert_called_once_with(engine)
        finally:
            engine.git.stop.set()
            for release in releases:
                release.set()
            service.join(5)
            if manual_thread.ident is not None:
                manual_thread.join(5)
        assert not service.is_alive() and not failures


def test_dispatch_failure_releases_shared_gate():
    scheduler = Scheduler(Cron("* * * * *"), "UTC", datetime(2026, 1, 1, tzinfo=UTC))
    dispatch = Mock(side_effect=RuntimeError("thread unavailable"))
    with pytest.raises(RuntimeError):
        scheduler.tick(datetime(2026, 1, 1, 0, 1, tzinfo=UTC), Mock(), dispatch)
    assert not scheduler.busy.locked()


@pytest.mark.parametrize(
    "code,probe,error",
    [
        ("print('a' * 280000)", "fsck", None),
        ("print('a' * 280000)", "config", "GIT_OUTPUT_LIMIT"),
        (
            "print('a' * 280000); print('error: Input/output error')",
            "fsck",
            "FILESYSTEM_ERROR",
        ),
        ("os.kill(os.getpid(), signal.SIGKILL)", "fsck", "GIT_INTERRUPTED"),
        ("time.sleep(10)", "fsck", "GIT_TIMEOUT"),
    ],
)
def test_probe_output_is_bounded_and_classifies_interruption(
    monkeypatch,
    tmp_path,
    code,
    probe,
    error,
):
    executable = tmp_path / "git"
    executable.write_text(f"#!{sys.executable}\nimport os, signal, time\n{code}\n")
    executable.chmod(0o755)
    monkeypatch.setattr("repoharbor.git_backup.shutil.which", lambda _: str(executable))
    checker = Git(
        Credentials({}), threading.Event(), timeout=0.3 if error == "GIT_TIMEOUT" else 5
    )
    if error:
        with pytest.raises(GitError, match=f"^{error}$"):
            checker.run([], probe=probe)
    else:
        assert checker.run([], probe=probe) == ""


def test_stop_terminates_running_probe(monkeypatch, tmp_path):
    executable = tmp_path / "git"
    started = tmp_path / "started"
    executable.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(started)!r}, 'w').write(str(os.getpid()))\ntime.sleep(10)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr("repoharbor.git_backup.shutil.which", lambda _: str(executable))
    checker = Git(Credentials({}), threading.Event(), timeout=5)

    def stop():
        eventually(started.exists)
        checker.stop.set()

    thread = threading.Thread(target=stop)
    thread.start()
    try:
        with pytest.raises(GitError, match="^INTERRUPTED$"):
            checker.run([], probe="fsck")
    finally:
        thread.join(6)
    with pytest.raises(ProcessLookupError):
        os.kill(int(started.read_text()), signal.SIGTERM)


@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES, errno.ENOSPC])
def test_mirror_filesystem_errors_are_uncheckable(factory, monkeypatch, error):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        stat = Path.lstat

        def lstat(path):
            if path == mirror:
                raise OSError(error, "private filesystem diagnostic")
            return stat(path)

        monkeypatch.setattr(Path, "lstat", lstat)
        integrity.run(engine)
        assert engine.store.data["integrity"]["uncheckable"] == 1
        assert engine.repos["demo"]["integrity"]["status"] == "healthy"
        assert (
            engine.repos["demo"]["integrity"]["periodic"]["error_code"]
            == "FILESYSTEM_ERROR"
        )


def test_integrity_status_text_json_and_legacy_fields(factory, monkeypatch, capsys):
    with factory() as engine:
        engine.run()
        config = replace(engine.config, integrity_check=True, timezone="Europe/Berlin")
        log = engine.store.data["scheduler"]["last_successful_run_at"]
        path = engine.path("demo") / "metadaten.json"
        monkeypatch.setattr(status, "request", lambda *_: Result.BUSY)

        def invoke(command, action=None, as_json=True):
            args = Namespace(
                command=command, action=action, repository="demo", json=as_json
            )
            assert status.print_status(config, args) == Result.SUCCESS
            output = capsys.readouterr().out
            return json.loads(output) if as_json else output

        assert (
            invoke("repositories", "show")["periodic_integrity"]["last_result"] is None
        )
        monkeypatch.setattr(
            integrity, "inspect", Mock(return_value=("corrupt", "GIT_CORRUPTION"))
        )
        integrity.run(engine)
        before = path.read_bytes()
        report = invoke("status")
        assert report["last_successful_run_at"] == status.timestamp(log)
        periodic = report["periodic_integrity"]
        assert periodic["corrupt"] == 1 and periodic["last_result"] == "issues"
        assert datetime.fromisoformat(periodic["next_scheduled_at"]).hour == 4
        assert "GIT_CORRUPTION" in invoke("repositories", "list", as_json=False)
        assert (
            invoke("repositories", "show")["integrity"]["last_check_kind"] == "periodic"
        )
        assert "corrupt" in invoke("status", as_json=False)
        assert path.read_bytes() == before

        with engine.store.transaction() as data:
            data["integrity"].update(state="running", current_repository="demo")
        assert invoke("status")["periodic_integrity"]["current_repository"] == "demo"
        monkeypatch.setattr(status, "request", lambda *_: Result.SERVICE_UNREACHABLE)
        assert invoke("status")["periodic_integrity"]["state"] == "interrupted"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"state": "running"},
        {"healthy": True},
        {"state": "oops"},
        {"last_started_at": "invalid"},
        {"last_completed_at": utcnow()},
        {"state": "running", "last_started_at": utcnow(), "current_repository": {}},
        {
            "state": "running",
            "last_started_at": utcnow(),
            "current_repository": "../unsafe",
        },
        {"current_repository": "demo"},
        {"healthy": 1},
    ],
)
def test_periodic_journal_rejects_invalid_schema(factory, bad):
    with factory() as engine, engine.store.transaction() as data:
        data["integrity"] = integrity_state() | bad if isinstance(bad, dict) else bad
    path = factory.root / "data.json"
    before = path.read_bytes()
    with pytest.raises(HarborError, match="Invalid journal"), factory():
        pass
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"unexpected": True},
        {"error_code": "raw diagnosis"},
        {"last_started_at": None},
        {"repair_pending": 1},
        {"damaged_at": "bad"},
    ],
)
def test_periodic_metadata_corruption_fails_closed(factory, bad):
    with factory() as engine:
        engine.run()
        integrity.run(engine)
        state = engine.repos["demo"]["integrity"]
        state["periodic"] = state["periodic"] | bad if isinstance(bad, dict) else bad
        engine.save_meta("demo")
        path = engine.path("demo") / "metadaten.json"
    before = path.read_bytes()
    with pytest.raises(HarborError, match="Invalid repository metadata"), factory():
        pass
    assert path.read_bytes() == before


def test_probe_interruption_and_directory_read_error(factory, monkeypatch):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        engine.git.stop.set()
        assert integrity.inspect(engine.git, mirror) == ("interrupted", None)
        engine.git.stop.clear()

        def inaccessible(path, onerror):
            assert path == mirror
            onerror(PermissionError(errno.EACCES, "private path"))

        monkeypatch.setattr("repoharbor.persistence.os.walk", inaccessible)
        assert integrity.inspect(engine.git, mirror) == (
            "uncheckable",
            "FILESYSTEM_ERROR",
        )


def test_integrity_timeout_is_visible_without_a_backup_error(factory, capsys):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        engine.git.timeout = 0
        integrity.run(engine)
        args = Namespace(
            command="repositories", action="show", repository="demo", json=True
        )
        assert status.print_status(engine.config, args) == Result.SUCCESS
        repo = json.loads(capsys.readouterr().out)
        assert repo["integrity"]["status"] == "healthy"
        assert repo["periodic_integrity"]["last_result"] == "uncheckable"
        assert repo["periodic_integrity"]["error_code"] == "GIT_TIMEOUT"
        assert repo["successful_updates"] == 1
        assert repo["last_error_code"] is None
        args.json = False
        assert status.print_status(engine.config, args) == Result.SUCCESS
        assert "GIT_TIMEOUT" in capsys.readouterr().out
        assert engine.store.data["integrity"]["uncheckable"] == 1


def test_signal_after_metadata_keeps_the_completed_repository_finding(factory):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        info = engine.repos["demo"]["integrity"]
        info["status"] = "unchecked"

        def terminate(point):
            if point == "integrity_metadata":
                engine.git.stop.set()

        engine.checkpoint = terminate
        integrity.run(engine)
        assert info["status"] == "healthy"
        assert info["periodic"]["last_completed_at"] is not None
        global_status = engine.store.data["integrity"]
        assert global_status["last_completed_at"] is None
        assert global_status["state"] == "interrupted"
