import threading
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from repoharbor.errors import HarborError
from repoharbor.persistence import read_json
from repoharbor.recovery import recover, repair
from repoharbor.scheduler import Cron, Scheduler


def test_interrupted_repair_does_not_consume_failure_budget(factory, monkeypatch):
    with factory() as engine:
        engine.run()
        meta = engine.repos["demo"]
        meta["integrity"]["status"] = "corrupt"

        def interrupted(*args, **kwargs):
            engine.git.stop.set()
            raise HarborError("INTERRUPTED")

        fetch = Mock(side_effect=interrupted)
        monkeypatch.setattr(engine.git, "fetch", fetch)
        repair(engine, "demo")
        assert fetch.call_count == 1
        assert meta["integrity"]["repair_attempts"] == 0
        assert meta["backup"]["status"] == "interrupted"
        assert engine.store.data["active_operation"]["state"] == "interrupted"


def test_owned_partial_retries_only_on_next_regular_run(factory):
    with factory() as engine:
        engine.store.begin("demo", "initial_clone")
        partial = engine.path("demo") / "repository.git.partial"
        partial.mkdir()
        (partial / "precious").write_text("retain me")
    with factory() as engine:
        recover(engine)
        assert not (engine.path("demo") / "repository.git").exists()
        assert not partial.exists()
        assert (
            engine.path("demo") / "quarantine/initial-clone/precious"
        ).read_text() == "retain me"
        assert engine.git.calls == []
        recover(engine)
        engine.run()
        assert engine.git.healthy(engine.path("demo") / "repository.git")


def test_finish_transfer_never_scans_tree(factory, monkeypatch):
    with factory() as engine:
        engine.budget.config = replace(engine.budget.config, global_bytes=1)
        scan = Mock(side_effect=AssertionError("finish must only check free space"))
        monkeypatch.setattr("repoharbor.storage.usage", scan)
        assert not engine.budget.interrupt("demo", engine.path("demo"))
        scan.assert_not_called()


def test_overlap_is_durable_before_active_run_finishes(factory):
    with factory() as engine:
        now = datetime(2026, 11, 12, tzinfo=UTC)
        scheduler = Scheduler(
            Cron("* * * * *"), "UTC", now, on_skip=engine.update_skips
        )
        scheduler.busy.acquire()
        try:
            scheduler.tick(datetime(2026, 11, 12, 0, 1, tzinfo=UTC), engine.run)
            assert (
                read_json(factory.root / "data.json")["scheduler"]["skipped_overlaps"]
                == 1
            )
        finally:
            scheduler.busy.release()


def test_transfer_monitor_throttles_inventory_and_prioritizes_reserve(
    factory, monkeypatch
):
    with factory() as engine:
        now = [0.0]
        status = Mock(return_value=None)
        monkeypatch.setattr(engine.budget, "transfer_status", status)
        monitor = engine.budget.monitor(
            "demo", engine.path("demo"), clock=lambda: now[0]
        )
        for _ in range(1000):
            now[0] += 0.001
            assert monitor() is False
        assert status.call_count == 1
        now[0] = 5.01
        assert monitor() is False
        assert status.call_count == 2
        now[0] = 10.1
        status.return_value = "FREE_SPACE_RESERVE"
        with pytest.raises(HarborError, match="FREE_SPACE_RESERVE"):
            monitor()


@pytest.mark.parametrize(
    "limit,expected",
    [("global", "global_limit_paused"), ("repo", "repo_limit_reached")],
)
def test_last_completed_repository_remeasures_and_resumes(
    factory, monkeypatch, limit, expected
):
    with factory() as engine:
        original = engine.git.clone
        cfg = engine.budget.config

        def over_budget(*args, **kwargs):
            original(*args, **kwargs)
            engine.budget.config = replace(
                cfg, **({"global_bytes": 0} if limit == "global" else {"repo_bytes": 0})
            )

        monkeypatch.setattr(engine.git, "clone", over_budget)
        engine.run()
        meta = engine.repos["demo"]
        assert meta["backup"]["successful_updates"] == 1
        assert meta["backup"]["status"] == expected
        assert engine.store.data["global_storage"]["status"] == (
            expected if limit == "global" else "available"
        )
        old = meta["backup"].copy()
        calls = len(engine.git.calls)
        engine.run()
        assert len(engine.git.calls) == calls
        assert meta["backup"]["last_checked_at"] == old["last_checked_at"]
        engine.budget.config = cfg
        engine.run()
        assert meta["backup"]["status"] == "up_to_date"
        assert meta["backup"]["last_updated_at"] == old["last_updated_at"]
        assert engine.store.data["global_storage"]["status"] == "available"


@pytest.mark.parametrize(
    "code,count,status",
    [
        ("GIT_FAILED", 1, "error"),
        ("ENOSPC", 1, "error"),
        ("INTERRUPTED", 0, "interrupted"),
        ("global_limit_paused", 0, "global_limit_paused"),
    ],
)
def test_local_error_keeps_proven_source_availability(
    factory, monkeypatch, code, count, status
):
    with factory() as engine:
        engine.run()
        meta = engine.repos["demo"]
        before = meta["backup"].copy()

        def fail(*args):
            raise HarborError(code)

        monkeypatch.setattr(engine.git, "refs", fail)
        engine.run()
        assert meta["source"]["status"] == "available"
        assert meta["backup"]["status"] == status
        assert meta["errors"]["total"] == count
        assert meta["backup"]["last_checked_at"] == before["last_checked_at"]
        assert meta["backup"]["last_updated_at"] == before["last_updated_at"]


def test_transaction_serializes_worker_and_scheduler(factory, monkeypatch):
    import repoharbor.persistence as persistence

    with factory() as engine:
        entered, release, skipped = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = persistence.atomic_json
        first = [True]
        failures = []

        def slow_write(path, data):
            if path.name == "data.json" and first[0]:
                first[0] = False
                entered.set()
                assert release.wait(5)
            original(path, data)

        monkeypatch.setattr(persistence, "atomic_json", slow_write)

        def work():
            try:
                engine.store.begin("demo", "initial_clone")
            except Exception as error:
                failures.append(error)

        def skip():
            try:
                skipped.set()
                engine.update_skips(1)
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=work)
        worker.start()
        assert entered.wait(5)
        observer = threading.Thread(target=skip)
        observer.start()
        assert skipped.wait(5)
        assert (
            observer.is_alive()
        )  # It cannot interleave with the pending journal write.
        release.set()
        worker.join(5)
        observer.join(5)
        assert not worker.is_alive() and not observer.is_alive() and not failures
        data = read_json(factory.root / "data.json")
        assert data["scheduler"]["skipped_overlaps"] == 1
        assert data["active_operation"]["state"] == "running"


def test_failed_transaction_rolls_back_memory_and_preserves_disk(factory, monkeypatch):
    import copy
    import errno

    import repoharbor.persistence as persistence

    with factory() as engine:
        before = copy.deepcopy(engine.store.data)
        with monkeypatch.context() as local:
            local.setattr(
                persistence,
                "atomic_json",
                Mock(side_effect=OSError(errno.EIO, "synthetic")),
            )
            with pytest.raises(OSError):
                engine.store.begin("demo", "initial_clone")
        assert engine.store.data == before
        assert read_json(factory.root / "data.json") == before
        engine.update_skips(1)
        assert read_json(factory.root / "data.json")["active_operation"] is None


def test_finish_reserve_and_abort_budget_checks(factory, monkeypatch):
    from collections import namedtuple

    disk = namedtuple("disk", "total used free")
    with factory() as engine:
        monkeypatch.setattr(
            "repoharbor.storage.shutil.disk_usage", lambda _: disk(10, 9, 1)
        )
        engine.budget.config = replace(engine.budget.config, min_free_bytes=2)
        assert engine.budget.interrupt("demo", engine.path("demo"))
        engine.budget.config = replace(
            engine.budget.config,
            min_free_bytes=0,
            global_bytes=0,
            during_transfer="abort",
        )
        assert engine.budget.interrupt("demo", engine.path("demo"))
        engine.budget.config = replace(engine.budget.config, during_transfer="finish")
        assert not engine.budget.interrupt("demo", engine.path("demo"))


def test_inventory_error_is_not_hidden(tmp_path, monkeypatch):
    from repoharbor.storage import usage

    def broken_walk(path, onerror):
        onerror(OSError(13, "synthetic"))
        return iter(())

    monkeypatch.setattr("repoharbor.storage.os.walk", broken_walk)
    with pytest.raises(OSError):
        usage(tmp_path)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("No space left on device", "ENOSPC"),
        ("fatal local object corruption", "GIT_FAILED"),
    ],
)
def test_git_diagnostics_only_expose_fixed_codes(
    tmp_path, monkeypatch, message, expected
):
    from repoharbor.credentials import Credentials
    from repoharbor.git_backup import Git

    executable = tmp_path / "synthetic-git"
    executable.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        f"sys.stderr.write({message!r} + ' fictional-secret')\nsys.exit(1)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr("repoharbor.git_backup.shutil.which", lambda _: str(executable))
    with pytest.raises(HarborError) as caught:
        Git(Credentials({}), threading.Event()).run(["fetch"])
    assert str(caught.value) == expected
    assert "fictional-secret" not in str(caught.value)
    assert caught.value.source_status is None


def test_runtime_quarantine_is_ignored():
    import subprocess

    result = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "manual/demo/quarantine/initial-clone/objects/pack/test",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_healthy_does_not_swallow_signal(factory):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        engine.git.stop.set()
        with pytest.raises(HarborError, match="INTERRUPTED"):
            engine.git.healthy(mirror)
        recover(engine)
        repair(engine, "demo")
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 0


def test_detached_mirror_head_is_supported(factory):
    from conftest import git

    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        oid = git("rev-parse", "HEAD", cwd=mirror)
        (mirror / "HEAD").write_text(oid + "\n")
        assert engine.git.head(mirror) == oid


def test_repair_result_replay_does_not_count_again(factory):
    import copy

    from repoharbor.recovery import repair_result

    with factory() as engine:
        engine.run()
        repair_result(engine, "demo", "synthetic-operation", False)
        before = copy.deepcopy(engine.repos["demo"])
        repair_result(engine, "demo", "synthetic-operation", False)
        assert engine.repos["demo"] == before


def test_repair_stops_if_budget_closes_after_failed_attempt(factory, monkeypatch):
    with factory() as engine:
        engine.run()
        status = Mock(side_effect=[None, "global_limit_paused"])
        monkeypatch.setattr(engine.budget, "status", status)
        monkeypatch.setattr(
            engine.git, "fetch", Mock(side_effect=HarborError("GIT_FAILED"))
        )
        repair(engine, "demo")
        assert engine.git.fetch.call_count == 1
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 1


@pytest.mark.parametrize(
    "code",
    ["INTERRUPTED", "FREE_SPACE_RESERVE", "global_limit_paused", "repo_limit_reached"],
)
def test_repair_pause_without_stop_event_does_not_consume_attempt(
    factory, monkeypatch, code
):
    with factory() as engine:
        engine.run()
        monkeypatch.setattr(engine.git, "fetch", Mock(side_effect=HarborError(code)))
        repair(engine, "demo")
        assert engine.git.fetch.call_count == 1
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 0
        assert engine.repos["demo"]["backup"]["status"] == "interrupted"


@pytest.mark.parametrize(
    "field,value",
    [
        ("partial_operation", 4),
        ("quarantine", []),
        (
            "quarantine",
            {"state": "pending", "operation_id": "demo", "relative_path": "../outside"},
        ),
    ],
)
def test_invalid_partial_ownership_metadata_fails_closed(factory, field, value):
    from repoharbor.persistence import atomic_json

    with factory() as engine:
        meta = engine.repos["demo"]
        meta["backup"][field] = value
        path = engine.path("demo") / "metadaten.json"
        atomic_json(path, meta)
    before = path.read_bytes()
    with pytest.raises(HarborError), factory():
        pass
    assert path.read_bytes() == before


def test_worker_io_failure_reports_code_without_private_exception(factory, monkeypatch):
    import errno

    from repoharbor import cli

    with factory() as engine:
        engine.config = replace(engine.config, schedule="* * * * *")
        stop = threading.Event()

        class Clock:
            count = 0

            @classmethod
            def now(cls, zone):
                cls.count += 1
                return datetime(2026, 11, 12, 3, min(cls.count, 3), tzinfo=UTC)

        monkeypatch.setattr(cli, "datetime", Clock)

        def fail():
            raise OSError(errno.ENOSPC, "fictional-secret-path")

        monkeypatch.setattr(engine, "run", fail)
        with pytest.raises(HarborError, match="ENOSPC") as caught:
            cli.serve(engine, stop)
        assert "fictional-secret-path" not in str(caught.value)


def test_repair_capacity_pause_resumes_at_future_run(factory, monkeypatch):
    from conftest import git

    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
        (mirror / "objects" / oid[:2] / oid[2:]).unlink()
        engine.repos["demo"]["integrity"]["status"] = "suspect"
        engine.save_meta("demo")
        normal = engine.budget.config
        engine.budget.config = replace(normal, global_bytes=0)
        engine.run()
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 0
        engine.budget.config = normal
        engine.run()
        assert engine.git.healthy(mirror)
        assert engine.repos["demo"]["integrity"]["successful_repairs"] == 1
