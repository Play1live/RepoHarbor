import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from repoharbor import cli, integrity
from repoharbor.control import request
from repoharbor.errors import GitError, Result


def eventually(predicate):
    deadline = time.monotonic() + 6
    while not predicate():
        assert time.monotonic() < deadline
        threading.Event().wait(0.01)


@pytest.mark.parametrize("owner", ["cron", "manual", "integrity"])
def test_retry_shares_execution_lock(factory, monkeypatch, owner):
    config = replace(
        factory.config,
        auto_retry=True,
        manual_run=True,
        schedule="2 * * * *" if owner == "cron" else "4 * * * *",
        integrity_check=True,
        integrity=replace(factory.config.integrity, schedule="2 * * * *"),
    )
    with factory(config) as engine:
        retry_clock = Mock(return_value=1000)
        engine.retries.clock = retry_clock
        clone = engine.git.clone
        monkeypatch.setattr(
            engine.git, "clone", Mock(side_effect=GitError("GIT_NETWORK"))
        )
        assert engine.run() == Result.BACKUP_FAILED
        monkeypatch.setattr(engine.git, "clone", clone)
        clock = Mock()
        clock.now.return_value = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
        monkeypatch.setattr(cli, "datetime", clock)
        started, release, retry_started, retry_release = (
            threading.Event() for _ in range(4)
        )

        def hold_operation(*args, **kwargs):
            started.set()
            assert release.wait(10)
            return Result.SUCCESS

        if owner == "integrity":
            monkeypatch.setattr(integrity, "run", hold_operation)
        else:
            monkeypatch.setattr(engine, "run", hold_operation)

        execute = engine.retries.execute

        def hold_retry(alias):
            retry_started.set()
            assert retry_release.wait(10)
            execute(alias)

        monkeypatch.setattr(engine.retries, "execute", hold_retry)
        failure = []

        def service_main():
            try:
                cli.serve(engine, engine.git.stop)
            except Exception as error:
                failure.append(error)

        service = threading.Thread(target=service_main)
        manual_result = []
        manual = threading.Thread(
            target=lambda: manual_result.append(request(config.root, b"repo demo"))
        )
        service.start()
        try:
            eventually(lambda: request(config.root, b"status") == Result.SUCCESS)
            if owner == "manual":
                manual.start()
            else:
                clock.now.return_value = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
            assert started.wait(6)
            retry_clock.return_value = 1300
            before = clock.now.call_count
            eventually(lambda: clock.now.call_count >= before + 2)
            assert not retry_started.is_set()
            release.set()
            assert retry_started.wait(6)
            assert request(config.root, b"repo demo") == Result.BUSY
            retry_release.set()
            eventually(lambda: request(config.root, b"status") == Result.SUCCESS)
            assert engine.repos["demo"]["retry"]["last_result"] == "succeeded"
            assert engine.repos["demo"]["backup"]["successful_updates"] == 1
            if owner == "manual":
                manual.join(5)
                assert manual_result == [Result.SUCCESS]
        finally:
            engine.git.stop.set()
            release.set()
            retry_release.set()
            service.join(10)
            if manual.ident is not None:
                manual.join(10)
        assert not service.is_alive() and not failure


@pytest.mark.parametrize("manual_run", [False, True])
def test_service_runs_due_retry_independently_of_manual_cli(
    factory, monkeypatch, manual_run
):
    config = replace(factory.config, auto_retry=True, manual_run=manual_run)
    with factory(config) as engine:
        original_clone = engine.git.clone
        clone = Mock(side_effect=GitError("GIT_SERVER_TEMPORARY"))
        monkeypatch.setattr(engine.git, "clone", clone)
        clock = Mock(return_value=1000)
        engine.retries.clock = clock
        schedule = Mock()
        schedule.now.return_value = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
        monkeypatch.setattr(cli, "datetime", schedule)
        if not manual_run:
            assert engine.run() == Result.BACKUP_FAILED
        service = threading.Thread(target=cli.serve, args=(engine, engine.git.stop))
        service.start()
        try:
            eventually(lambda: request(config.root, b"status") == Result.SUCCESS)
            assert request(config.root, b"repo demo") == (
                Result.BACKUP_FAILED if manual_run else Result.FEATURE_DISABLED
            )
            clone.assert_called_once()
            assert engine.repos["demo"]["retry"]["status"] == "pending"
            monkeypatch.setattr(engine.git, "clone", original_clone)
            clock.return_value = 1300
            eventually(
                lambda: engine.repos["demo"]["retry"]["status"] == "succeeded"
                and request(config.root, b"status") == Result.SUCCESS
            )
            assert engine.repos["demo"]["backup"]["successful_updates"] == 1
        finally:
            engine.git.stop.set()
            service.join(10)
        assert not service.is_alive()
