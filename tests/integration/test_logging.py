import errno
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import git

from repoharbor import integrity, log_files
from repoharbor.config import LogFile, Logging
from repoharbor.errors import GitError, Result
from repoharbor.events import Events
from repoharbor.github import DiscoveryError
from repoharbor.recovery import recover


def parse(stream):
    return [json.loads(line) for line in stream.splitlines()]


@pytest.mark.parametrize("provider", ["github_env", "gitlab_env"])
def test_provider_summaries_and_safe_failures(provider, request, capsys):
    fixture = request.getfixturevalue(provider)
    config = replace(
        fixture.config, extended_logging=True, logging=Logging("DEBUG", "json")
    )
    events = Events(config)
    with fixture.create(config, git_instance=fixture.git(), events=events) as engine:
        assert not engine.discover()
        fixture.client.discover.side_effect = DiscoveryError(
            "https://example.org/?Authorization=fictional-token",
            temporary=True,
        )
        assert engine.discover()
        prefix = config.providers[0].type.upper()
        fixture.client.discover.side_effect = DiscoveryError(
            f"{prefix}_RATE_LIMIT",
            253402300799,
            temporary=True,
        )
        assert engine.discover() and engine.discover()
    events.close()
    raw = capsys.readouterr().err
    records = parse(raw)
    (completed,) = (
        record
        for record in records
        if record["event"] == "discovery.provider_completed"
    )
    assert completed["provider"] == config.providers[0].type
    assert completed["authority"] == config.providers[0].host
    assert completed["detected"] == 1 and completed["considered"] == 1
    (failed,) = (record for record in records if record["event"] == "discovery.failed")
    assert failed["temporary"] and failed["error_code"] == "OPERATION_FAILED"
    names = [record["event"] for record in records]
    assert names.count("discovery.rate_limited") == 1
    assert names.count("discovery.cooldown") == 1
    assert "fictional-token" not in raw and "Authorization" not in raw


def test_backup_retry_integrity_recovery_events(factory, monkeypatch, capsys, remote):
    config = replace(
        factory.config,
        extended_logging=True,
        logging=Logging("DEBUG", "json"),
        auto_retry=True,
    )
    events = Events(config)
    clock = Mock(return_value=1000)
    with factory(config, events=events) as engine:
        engine.retries.clock = clock
        with monkeypatch.context() as patch:
            patch.setattr(
                engine.git, "clone", Mock(side_effect=GitError("GIT_NETWORK"))
            )
            assert engine.run(manual=True, repository="demo") == Result.BACKUP_FAILED
        clock.return_value = 1300
        engine.retries.execute("demo")
        assert engine.repos["demo"]["retry"]["status"] == "succeeded"
        assert engine.run() == Result.SUCCESS
        work, _ = remote
        (work / "hello.txt").write_text("next")
        git("commit", "-am", "next", cwd=work)
        git("push", "origin", "main", cwd=work)
        assert engine.run() == Result.SUCCESS
        integrity.run(engine)
        with monkeypatch.context() as patch:
            patch.setattr(
                integrity, "inspect", lambda *_: ("corrupt", "GIT_CORRUPTION")
            )
            integrity.run(engine)
        assert engine.repos["demo"]["integrity"]["periodic"]["repair_pending"]
        with monkeypatch.context() as patch:
            patch.setattr("repoharbor.recovery.verify", Mock(side_effect=[False, True]))
            recover(engine, during_run=True, periodic=True)
        assert not engine.repos["demo"]["integrity"]["periodic"]["repair_pending"]
        engine.update_skips(1)
        integrity.skip_overlap(engine.store, 1, events)
    events.close()
    records = parse(capsys.readouterr().err)
    names = {record["event"] for record in records}
    assert {
        "backup.run_started",
        "backup.run_completed",
        "backup.clone_started",
        "backup.update_started",
        "backup.completed",
        "backup.up_to_date",
        "backup.failed",
        "retry.scheduled",
        "retry.started",
        "retry.succeeded",
        "integrity.started",
        "integrity.completed",
        "integrity.mirror_checked",
        "integrity.mirror_damaged",
        "recovery.incomplete_operation",
        "recovery.mirror_damaged",
        "recovery.completed",
        "repair.started",
        "repair.completed",
        "backup.schedule_skipped",
        "integrity.schedule_skipped",
    } <= names
    (manual,) = (
        r
        for r in records
        if r["event"] == "backup.run_started" and r["trigger"] == "manual"
    )
    rid = manual["run_id"]
    assert [r["event"] for r in records if r.get("run_id") == rid] == [
        "backup.run_started",
        "discovery.started",
        "discovery.completed",
        "backup.check_started",
        "backup.clone_started",
        "retry.scheduled",
        "backup.failed",
        "backup.run_completed",
    ]
    started = next(r for r in records if r["event"] == "backup.update_started")
    (finished,) = (
        r
        for r in records
        if r["event"] == "backup.completed" and r["run_id"] == started["run_id"]
    )
    assert started["operation_id"] == finished["operation_id"]
    assert finished["duration_ms"] >= 0


def test_rotation_failure_after_git_preserves_journal_and_backup(
    factory, log_mount, monkeypatch, capsys
):
    config = replace(
        factory.config,
        extended_logging=True,
        logging=Logging("DEBUG", "json", LogFile(True, log_mount / "events.log", 1, 2)),
    )
    events = Events(config)
    events.open_file(config)
    file = events.file
    file.maximum = 220
    fictional = "fictional-Credential-Not-A-Secret"
    real_write = log_files.os.write
    rotate = file.rotate

    with factory(config, events=events) as engine:

        def fail_rotate():
            if engine.store.data["active_operation"] is not None:
                raise OSError(errno.EIO, fictional)
            rotate()

        monkeypatch.setattr(file, "rotate", fail_rotate)

        def write(fd, payload):
            if fd == file.file:
                assert fictional.encode() not in payload
            return real_write(fd, payload)

        monkeypatch.setattr(log_files.os, "write", write)
        engine.git.credentials.values["fictional"] = fictional
        assert engine.run() == Result.SUCCESS
        assert engine.store.data["active_operation"]["state"] == "completed"
        assert engine.repos["demo"]["errors"]["consecutive"] == 0
        assert engine.repos["demo"]["backup"]["successful_updates"] == 1
        assert engine.path("demo").joinpath("repository.git", "HEAD").is_file()
    events.close()
    captured = capsys.readouterr()
    assert fictional not in captured.out + captured.err
    names = [r["event"] for r in parse(captured.err)]
    assert names.count("logging.file_failed") == 1
    assert "backup.failed" not in names
    assert "backup.completed" in names
    assert list(log_mount.glob("*.log.1"))
    for path in log_mount.iterdir():
        assert fictional not in path.read_text()
        assert parse(path.read_text())


def test_interrupted_partial_is_logged_and_quarantined(factory, monkeypatch, capsys):
    config = replace(
        factory.config, extended_logging=True, logging=Logging("DEBUG", "json")
    )
    events = Events(config)
    with factory(config, events=events) as engine:

        def clone(_url, partial, *args):
            partial.mkdir()
            (partial / "marker").write_text("local recovery evidence")
            raise GitError("GIT_TIMEOUT")

        monkeypatch.setattr(engine.git, "clone", clone)
        assert engine.run() == Result.BACKUP_FAILED
        operation_id = engine.store.data["active_operation"]["id"]
        recover(engine)
        assert engine.path("demo").joinpath(
            "quarantine/initial-clone/marker"
        ).read_text() == ("local recovery evidence")
        assert engine.store.data["active_operation"]["state"] == "reconciled"
    events.close()
    records = parse(capsys.readouterr().err)
    incomplete = next(
        r for r in records if r["event"] == "recovery.incomplete_operation"
    )
    assert incomplete["operation_id"] == operation_id
    quarantine = [r for r in records if r["event"].startswith("recovery.quarantine_")]
    assert [r["event"] for r in quarantine] == [
        "recovery.quarantine_started",
        "recovery.quarantine_preserved",
    ]
    assert quarantine[0]["operation_id"] == quarantine[1]["operation_id"]


def test_retry_deferral_failure_and_exhaustion(factory, monkeypatch, capsys):
    config = replace(
        factory.config,
        extended_logging=True,
        auto_retry=True,
        retry=replace(factory.config.retry, max_retries=1),
        logging=Logging("DEBUG", "json"),
    )
    events = Events(config)
    clock = Mock(return_value=1000)
    with factory(config, events=events) as engine:
        engine.retries.clock = clock
        monkeypatch.setattr(
            engine.git, "clone", Mock(side_effect=GitError("GIT_NETWORK"))
        )
        assert engine.run() == Result.BACKUP_FAILED
        state = engine.repos["demo"]["retry"]
        engine.retries.defer_discovery("demo", cooldown=1320, checked=False)
        engine.retries.defer_discovery("demo", checked=True)
        assert state["attempts"] == 0
        clock.return_value = state["next_attempt_at"]
        engine.retries.execute("demo")
        assert state["status"] == "exhausted" and state["attempts"] == 1
    events.close()

    records = parse(capsys.readouterr().err)
    assert {
        "retry.cooldown_deferred",
        "retry.discovery_deferred",
        "retry.failed",
        "retry.limit_reached",
    } <= {r["event"] for r in records}
    assert all(r.get("error_code") != "OPERATION_FAILED" for r in records)
