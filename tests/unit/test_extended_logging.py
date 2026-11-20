import errno
import json
import logging
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from repoharbor import cli, log_files
from repoharbor.config import Config, LogFile, Logging, load
from repoharbor.errors import HarborError
from repoharbor.events import Events
from repoharbor.log_files import LogSetupError


def settings(options, **env):
    return load(
        {
            "REPOHARBOR_TOKEN_SOURCE": "env",
            "REPOHARBOR_CONFIG": yaml.safe_dump({"schema_version": 1, **options}),
            **env,
        }
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_defaults(enabled):
    config = settings(
        {"features": {"extended_logging": enabled, "restore_helper": True}}
    )
    assert config.extended_logging == enabled
    assert config.restore_helper
    assert config.logging == Logging()
    events = Events(config)
    events.open_file(config)
    assert events.file is None
    events.close()


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
@pytest.mark.parametrize("output", ["text", "json"])
def test_supported_settings(level, output):
    config = settings({"logging": {"level": level, "format": output}})
    assert config.logging == Logging(level, output)


@pytest.mark.parametrize(
    "options",
    [
        {"features": {"extended_logging": 1}},
        {"logging": None},
        {"logging": {"file": None}},
        {"logging": {"verbose": True}},
        {"logging": {"level": "TRACE"}},
        {"logging": {"level": ["INFO"]}},
        {"logging": {"format": True}},
        {"logging": {"format": "yaml"}},
        *(
            {"logging": {"file": values}}
            for values in (
                {"enabled": True},
                {"enabled": 0},
                {"unknown": True},
                {"max_size_mib": -1},
                {"max_size_mib": 0},
                {"max_size_mib": True},
                {"max_size_mib": 101},
                {"max_size_mib": 1.5},
                {"backup_count": False},
                {"backup_count": 0},
                {"backup_count": -1},
                {"backup_count": 21},
                {"backup_count": "1"},
                {"path": "/logs/a\x00.log"},
                {"path": "logs/test.log"},
                {"path": "/logs/../test.log"},
                {"path": "/logs/./test.log"},
                {"path": "/logs//test.log"},
                {"path": "/logs/"},
                {"path": 5},
                {"path": "/logs/" + "a" * 1024 + ".log"},
                {"path": "/logs/test"},
                {"path": "/backups/logs/test.log"},
                {"path": "/run/secrets/repoharbor/test.log"},
                {"path": "/run/secrets/test.log"},
            )
        ),
    ],
)
def test_strict_settings_even_when_disabled(options):
    with pytest.raises(HarborError):
        settings(options)


def test_custom_protected_path_and_legacy_root():
    with pytest.raises(HarborError, match="protected"):
        settings(
            {"logging": {"file": {"path": "/example/storage/records/test.log"}}},
            REPOHARBOR_BACKUP_ROOT="/example/storage",
        )
    assert settings({}, REPOHARBOR_BACKUP_ROOT="/logs").root == Path("/logs")


@pytest.fixture
def files(tmp_path, log_mount):
    backup = tmp_path / "backup"
    backup.mkdir()
    return Config(
        (),
        (),
        "env",
        root=backup,
        extended_logging=True,
        logging=Logging(
            "DEBUG", "json", LogFile(True, log_mount / "example.log", 1, 2)
        ),
    )


def entries(stream):
    return [json.loads(line) for line in stream.splitlines()]


def test_json_clock_level_fields_and_legacy(monkeypatch, capsys, caplog):
    config = Config(
        (), (), "env", extended_logging=True, logging=Logging("WARNING", "json")
    )
    events = Events(config)
    clock = Mock(side_effect=[12.0, 13.25])
    monkeypatch.setattr("repoharbor.events.time", SimpleNamespace(monotonic=clock))
    with events.run() as started:
        events.log("backup.started")
        events.log(
            "backup.failed",
            logging.WARNING,
            repository="example",
            error_code="SENSITIVE_RAW_DIAGNOSTIC",
            provider="https://example.org/private",
            token="fictional-auth-value",
            detected=2**100,
            due_at=float("nan"),
            started=started,
        )
    events.close()
    captured = capsys.readouterr()
    (data,) = entries(captured.err)
    assert data["level"] == "WARNING"
    assert data["event"] == "backup.failed"
    assert data["repository"] == "example"
    assert data["error_code"] == "OPERATION_FAILED"
    assert data["duration_ms"] == 1250
    assert datetime.fromisoformat(data["timestamp"]).utcoffset().total_seconds() == 0
    assert data["timestamp"].endswith("Z") and len(data["run_id"]) == 32
    assert not {"provider", "token", "detected", "due_at"} & data.keys()
    assert not captured.out
    assert not caplog.records
    Events().log("ignored", logging.WARNING, legacy=("old %s", "format"))
    assert caplog.messages == ["old format"]


def test_text_format(capsys):
    events = Events(Config((), (), "env", extended_logging=True))
    events.log("service.ready", result="success")
    events.close()
    line = capsys.readouterr().err
    assert "Z level=INFO event=service.ready result=success\n" in line


def test_rotation_retention_restart_and_permissions(files, capsys):
    events = Events(files)
    events.open_file(files)
    events.open_file(files)
    assert len(events.logger.handlers) == 2
    events.file.maximum = 240
    active = files.logging.file.path
    for attempt in range(11):
        events.log("backup.completed", repository="test-alias", attempts=attempt)
    inode = active.stat().st_ino
    previous = active.read_bytes()
    events.close()
    assert entries(previous)[-1]["attempts"] == 10
    assert [
        entries(path.read_bytes())[-1]["attempts"]
        for path in sorted(active.parent.glob("*.log.*"))
    ] == [9, 8]
    assert stat.S_IMODE(active.stat().st_mode) == 0o600
    assert (active.parent / "example.log.1").stat().st_mode & 0o777 == 0o600

    events = Events(files)
    events.open_file(files)
    events.log("service.ready")
    events.close()
    assert active.stat().st_ino == inode
    assert active.read_bytes().startswith(previous)
    assert len(entries(capsys.readouterr().err)) == 12

    files = replace(
        files,
        logging=replace(
            files.logging,
            file=replace(
                files.logging.file,
                backup_count=1,
            ),
        ),
    )
    events = Events(files)
    events.open_file(files)
    events.close()
    assert sorted(p.name for p in active.parent.iterdir()) == [
        "example.log",
        "example.log.1",
    ]


@pytest.mark.parametrize("target", ["active", "archive"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "fifo", "directory"])
def test_refuse_unsafe_entries(files, target, kind):
    active = files.logging.file.path
    entry = active if target == "active" else active.with_suffix(".log.2")
    state = files.root / "data.json"
    state.write_text("protected")
    state.chmod(0o600)
    if kind == "symlink":
        entry.symlink_to(state)
    elif kind == "hardlink":
        entry.hardlink_to(state)
    elif kind == "mode":
        entry.touch()
        entry.chmod(0o644)
    elif kind == "fifo":
        os.mkfifo(entry)
    else:
        entry.mkdir()
    events = Events(files)
    with pytest.raises(LogSetupError, match="regular files"):
        events.open_file(files)
    events.close()
    assert state.read_text() == "protected"


def test_mount_and_directory_validation(files, monkeypatch):
    logs = files.logging.file.path.parent
    real = log_files.read_mounts(None)
    mount = real[logs]
    events = Events(files)
    for mounts, code in (
        ({Path("/"): real[Path("/")]}, "LOG_MOUNT_MISSING"),
        ({**real, logs: mount._replace(filesystem=b"tmpfs")}, "LOG_MOUNT_UNSAFE"),
        (
            {**real, logs: mount._replace(root=files.root / "subdirectory")},
            "LOG_MOUNT_UNSAFE",
        ),
        ({**real, logs: mount._replace(root=files.root.parent)}, "LOG_MOUNT_UNSAFE"),
        ({**real, logs: mount._replace(id=b"0")}, "LOG_MOUNT_UNSAFE"),
    ):
        with monkeypatch.context() as context:
            context.setattr(log_files, "read_mounts", lambda _, mounts=mounts: mounts)
            with pytest.raises(LogSetupError) as failure:
                events.open_file(files)
            assert failure.value.code == code
    logs.chmod(0o777)
    with pytest.raises(LogSetupError, match="privately writable"):
        events.open_file(files)
    logs.chmod(0o500)
    with pytest.raises(LogSetupError, match="inaccessible"):
        events.open_file(files)
    logs.rmdir()
    logs.symlink_to(files.root, target_is_directory=True)
    with pytest.raises(LogSetupError, match="inaccessible"):
        events.open_file(files)
    events.close()
    assert not list(files.root.iterdir())


@pytest.mark.parametrize("suffix", ["", ".2"])
def test_reject_separately_mounted_log_entries(files, monkeypatch, suffix):
    mounts = log_files.read_mounts(None)
    active = files.logging.file.path
    entry = active.with_name(active.name + suffix)
    mounts[entry] = mounts[active.parent]
    monkeypatch.setattr(log_files, "read_mounts", lambda _: mounts)
    events = Events(files)
    with pytest.raises(LogSetupError, match="separate mount points"):
        events.open_file(files)
    events.close()
    assert not active.exists()


def test_resolved_secret_directory_and_file_mount(files, monkeypatch):
    logs = files.logging.file.path.parent
    actual = log_files.mount_id
    secret_alias = files.root.parent / "secret-alias"
    secret_alias.symlink_to(logs)
    events = Events(files)
    with pytest.raises(LogSetupError, match="protected storage"):
        events.open_file(replace(files, secrets_dir=secret_alias))
    monkeypatch.setattr(
        log_files,
        "mount_id",
        lambda fd: actual(fd) if stat.S_ISDIR(os.fstat(fd).st_mode) else b"other",
    )
    with pytest.raises(LogSetupError, match="directory mount"):
        events.open_file(files)
    events.close()


@pytest.mark.parametrize("failure", ["write", "short", "rotate", "replaced"])
def test_file_failure_diagnoses_once_and_keeps_console(
    files, monkeypatch, capsys, failure
):
    events = Events(files)
    events.open_file(files)
    events.log("service.started")
    active = files.logging.file.path
    first = active.read_bytes()
    with monkeypatch.context() as context:
        if failure == "write":
            write = Mock(side_effect=OSError(errno.ENOSPC, "fictional-sensitive-path"))
            context.setattr(log_files.os, "write", write)
        elif failure == "short":
            write = os.write
            context.setattr(log_files.os, "write", lambda fd, data: write(fd, data[:8]))
        elif failure == "rotate":
            events.file.maximum = len(first)
            context.setattr(
                log_files.os, "replace", Mock(side_effect=OSError(errno.EIO))
            )
        else:
            events.file.maximum = len(first)
            active.rename(active.with_suffix(".old"))
            active.touch(mode=0o600)
        events.log("service.ready")
        events.log("service.stopped")
        if failure == "write":
            write.assert_called_once()
    assert events.file.file is None
    events.close()
    records = entries(capsys.readouterr().err)
    assert [r["event"] for r in records] == [
        "service.started",
        "service.ready",
        "logging.file_failed",
        "service.stopped",
    ]
    if failure != "replaced":
        assert active.read_bytes() == first


def test_concurrent_emission_and_exclusive_directory_lock(files, capsys):
    events = Events(files)
    events.open_file(files)
    other = Events(files)
    with pytest.raises(LogSetupError, match="in use"):
        other.open_file(files)
    with ThreadPoolExecutor(4) as workers:
        tuple(
            workers.map(lambda n: events.log("backup.completed", attempts=n), range(40))
        )
    events.close()
    other.open_file(files)
    other.close()
    file_records = entries(files.logging.file.path.read_bytes())
    assert sorted(r["attempts"] for r in file_records) == list(range(40))
    assert sorted(
        entries(capsys.readouterr().err), key=lambda event: event["attempts"]
    ) == sorted(file_records, key=lambda event: event["attempts"])


def test_cli_missing_log_mount_precedes_backup_state(monkeypatch, capsys, tmp_path):
    root = tmp_path / "backups"
    monkeypatch.setattr("sys.argv", ["repoharbor"])
    monkeypatch.setenv("REPOHARBOR_TOKEN_SOURCE", "env")
    monkeypatch.setenv("REPOHARBOR_BACKUP_ROOT", str(root))
    monkeypatch.setenv(
        "REPOHARBOR_CONFIG",
        "schema_version: 1\nfeatures: {extended_logging: true}\n"
        "logging: {format: json, file: {enabled: true, path: /nonexistent/test.log}}",
    )
    assert cli.main() == 1
    (record,) = entries(capsys.readouterr().err)
    assert record["event"] == "service.startup_failed"
    assert record["error_code"] == "LOG_MOUNT_MISSING"
    assert not root.exists()
