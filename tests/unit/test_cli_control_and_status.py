import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from repoharbor import cli, control
from repoharbor.config import load
from repoharbor.errors import HarborError, Result
from repoharbor.persistence import atomic_json, read_json
from repoharbor.scheduler import Cron, Scheduler
from repoharbor.status import global_view, repo_view


def test_manual_flag_validation():
    env = {"REPOHARBOR_TOKEN_SOURCE": "env"}
    assert not load(env | {"REPOHARBOR_CONFIG": "schema_version: 1"}).manual_run
    for setting in ("true", "false", "null", "'true'", "1"):
        env["REPOHARBOR_CONFIG"] = (
            f"schema_version: 1\nfeatures: {{manual_run: {setting}}}"
        )
        if setting in ("true", "false"):
            assert load(env).manual_run is (setting == "true")
        else:
            with pytest.raises(HarborError):
                load(env)
    env["REPOHARBOR_CONFIG"] = (
        "schema_version: 1\nfeatures: {manual_run: true, web_ui: true}"
    )
    with pytest.raises(HarborError):
        load(env)


@pytest.mark.parametrize(
    "args",
    [
        ("backup", "run"),
        ("backup", "run", "--all", "--repository", "demo"),
        ("backup",),
        ("repositories",),
        ("repositories", "show"),
        ("status", "--extra"),
    ],
)
def test_invalid_cli_arguments(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["repoharbor", *args])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == Result.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "selector",
    ["../demo", "/tmp/demo", "https://example.org/demo", "gitlab:gitlab.com:01"],
)
def test_invalid_cli_selectors(monkeypatch, selector):
    for args in (["repositories", "show"], ["backup", "run", "--repository"]):
        monkeypatch.setattr(sys, "argv", ["repoharbor", *args, selector])
        assert cli.main() == Result.INVALID_ARGUMENT


def test_disabled_and_offline_cli(factory, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["repoharbor", "backup", "run", "--all"])
    monkeypatch.setattr(cli, "load", lambda _: factory.config)
    assert cli.main() == Result.FEATURE_DISABLED
    monkeypatch.setattr(cli, "load", lambda _: replace(factory.config, manual_run=True))
    assert cli.main() == Result.SERVICE_UNREACHABLE
    assert not list(factory.root.iterdir())


def test_read_only_cli_reports_corruption_without_leaks(factory, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load", lambda _: factory.config)
    monkeypatch.setattr(cli.Credentials, "resolve", Mock(side_effect=AssertionError))
    monkeypatch.setattr(
        "repoharbor.status.shutil.get_terminal_size", lambda: os.terminal_size((26, 30))
    )

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["repoharbor", *args])
        return cli.main(), capsys.readouterr().out

    with factory() as engine:
        assert run("repositories", "show", "missing")[0] == Result.UNKNOWN_REPOSITORY
        folder = engine.path("demo")
        meta_path = folder / "metadaten.json"
        meta = read_json(meta_path)
        token = "fictional-token-do-not-print"
        meta["repository"]["web_url"] = f"https://user:{token}@example.org/repo"
        meta["errors"]["consecutive"] = 2
        meta["errors"]["last"] = {"code": "GIT_FAILED", "message": token, "at": token}
        meta["errors"]["recent"] = [meta["errors"]["last"], None]
        atomic_json(meta_path, meta)
        state = factory.root / "data.json"
        files = {p: p.read_bytes() for p in (meta_path, state)}
        code, text = run("repositories", "list")
        assert code == 0
        assert all(len(line) <= 26 for line in text.splitlines())
        assert "demo" in text and "GIT_FAILED" in text
        code, details = run("repositories", "show", "demo", "--json")
        assert code == 0 and token not in details
        details = json.loads(details)
        assert details["selection_status"] == "included"
        assert details["web_url"] is None
        assert details["errors"]["last"] == {"at": None, "code": "GIT_FAILED"}
        assert details["mirror_path"] == "manual/demo/repository.git"
        assert details["integrity"]["status"] == "unchecked"
        assert run("repositories", "show", "demo")[0] == Result.SUCCESS
        assert run("repositories", "list", "--json")[0] == Result.SUCCESS
        code, text = run("status")
        assert code == 0 and "unreachable" in text and "unknown" in text
        code, state_json = run("status", "--json")
        assert code == 0
        assert json.loads(state_json)["repositories"]["with_errors"] == 1
        assert {p: p.read_bytes() for p in files} == files
        meta_path.unlink()
        os.mkfifo(meta_path, 0o600)
        assert run("repositories", "show", "demo")[0] == Result.STATE_UNAVAILABLE
        code, text = run("repositories", "list", "--json")
        assert code == Result.STATE_UNAVAILABLE and json.loads(text)["unreadable"] == 1
        state.write_text(token)
        code, text = run("status", "--json")
        assert code == Result.STATE_UNAVAILABLE and token not in text
        assert json.loads(text)["repositories"]["included"] is None


def test_status_without_safe_repository_catalog(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load", lambda _: factory.config)
    with factory():
        pass
    (factory.root / "gitlab").symlink_to(tmp_path)
    monkeypatch.setattr(sys, "argv", ["repoharbor", "repositories", "show", "demo"])
    assert cli.main() == Result.STATE_UNAVAILABLE


def test_status_configured_provider_errors_and_current_limits(gitlab_env):
    env = gitlab_env
    with env.create(env.config, git_instance=env.git()) as engine:
        assert engine.run() == Result.SUCCESS
        code = {
            "state": "failed",
            "code": "GITLAB_INCOMPLETE_PAGINATION",
            "started_at": "2026-01-01T00:00:00Z",
            "retry_at": 10**30,
        }
        data = engine.store.data
        data["provider_discovery"]["gitlab:gitlab.example.org"] = code
        discovery = global_view(env.config, data, [], 0)["discovery"]
        assert discovery["gitlab:gitlab.example.org"]["error_code"] == code["code"]
        assert discovery["gitlab:gitlab.example.org"]["retry_at"] is None
        assert discovery["github:github.com"]["configured"] is False
        view = repo_view(env.config, env.key, engine.repos[env.key], True)
        assert view["discovery_status"] == "available"
        assert view["web_url"] == "https://gitlab.example.org/fictional/project"
        assert view["limit_bytes"] is None


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 3, 29, 0, 0, tzinfo=UTC), "2026-03-30T02:30:00+02:00"),
        (datetime(2026, 10, 25, 0, 10, tzinfo=UTC), "2026-10-25T02:30:00+02:00"),
        (datetime(2026, 10, 25, 0, 45, tzinfo=UTC), "2026-10-26T02:30:00+01:00"),
        (datetime(2026, 10, 25, 1, 10, tzinfo=UTC), "2026-10-26T02:30:00+01:00"),
    ],
)
def test_next_cron_respects_dst(now, expected):
    assert (
        Cron("30 2 * * *").next_after(now, ZoneInfo("Europe/Berlin")).isoformat()
        == expected
    )


def test_impossible_cron_date():
    assert Cron("5 5 31 2 *").next_after(datetime.now(UTC), ZoneInfo("UTC")) is None


def test_control_rejects_bad_frames_and_enforces_daemon_flag(factory):
    with factory() as engine:
        scheduler = Scheduler(Cron("* * * * *"), "UTC", datetime.now(UTC))
        monitor = control.Control(engine, scheduler, engine.git.stop)
        with monitor.start():
            for command in (b"invalid", b"repo \xff", b"repo " + b"a" * 320):
                assert control.request(factory.root, command) == Result.INVALID_ARGUMENT
            assert control.request(factory.root, b"all") == Result.FEATURE_DISABLED
            with pytest.raises(OSError), control.listen(factory.root):
                pass
        assert control.request(factory.root, b"status") == Result.SERVICE_UNREACHABLE
        with control.listen(factory.root):
            pass


def test_peer_credentials_checked_before_io(factory, monkeypatch):
    with factory() as engine:
        scheduler = Scheduler(Cron("* * * * *"), "UTC", datetime.now(UTC))
        monitor = control.Control(engine, scheduler, Event())
        impostor = Mock()
        impostor.getsockopt.return_value = control.struct.pack(
            "iII", 42, os.geteuid() + 1, 500
        )
        assert not monitor.handle(impostor)
        impostor.recv.assert_not_called()
        impostor.__enter__ = lambda _: impostor
        impostor.__exit__ = lambda *_: None
        monkeypatch.setattr(control.socket, "socket", Mock(return_value=impostor))
        assert control.request(factory.root, b"all") == Result.SERVICE_UNREACHABLE
        impostor.sendall.assert_not_called()


def test_control_stopped_socket_error_and_fatal_worker(factory, monkeypatch):
    with factory(replace(factory.config, manual_run=True)) as engine:
        scheduler = Scheduler(Cron("* * * * *"), "UTC", datetime.now(UTC))
        monitor = control.Control(engine, scheduler, engine.git.stop)
        connection = Mock()
        connection.getsockopt.return_value = control.struct.pack(
            "iII", 1, os.geteuid(), 1
        )
        connection.recv.side_effect = TimeoutError
        assert not monitor.handle(connection)
        monitor.stop.set()
        connection.recv.side_effect = None
        connection.recv.return_value = b"all"
        assert not monitor.handle(connection)
        connection.sendall.assert_called_once_with(b"5")
        monitor.stop.clear()
        monkeypatch.setattr(
            engine, "run", Mock(side_effect=OSError("fictional secret"))
        )
        with monitor.start():
            assert control.request(factory.root, b"all") == Result.BACKUP_FAILED
        assert monitor.stop.is_set()
        assert not scheduler.busy.locked()


def test_control_thread_start_and_accept_failures(factory, monkeypatch):
    with factory(replace(factory.config, manual_run=True)) as engine:
        scheduler = Scheduler(Cron("* * * * *"), "UTC", datetime.now(UTC))
        monitor = control.Control(engine, scheduler, Event())
        server, connection = Mock(), Mock()
        server.accept.return_value = (connection, None)
        connection.recv.return_value = b"repo demo"
        monkeypatch.setattr(control, "same_user", lambda _: True)
        monkeypatch.setattr(control.threading, "Thread", Mock(side_effect=None))
        control.threading.Thread.return_value.start.side_effect = RuntimeError
        monitor.accept(server)
        assert isinstance(monitor.failure, RuntimeError)
        assert monitor.stop.is_set() and not scheduler.busy.locked()
        connection.close.assert_called_once()


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("backup", "partial_operation", []),
        ("backup", "format", "bundle"),
        ("backup", "quarantine", {"state": "bad"}),
        ("backup", "successful_updates", "two"),
        ("errors", "consecutive", -1),
        ("errors", "recent", [None] * 21),
        ("repository", "id", "other"),
    ],
)
def test_read_metadata_rejects_inconsistent_fields(factory, section, field, value):
    from repoharbor.catalog import read_metadata

    with factory() as engine:
        folder = engine.path("demo")
        path = folder / "metadaten.json"
        meta = read_json(path)
        meta[section][field] = value
        atomic_json(path, meta)
        before = path.read_bytes()
        with pytest.raises(HarborError):
            read_metadata("demo", folder)
        assert path.read_bytes() == before


def test_journal_rejects_deep_json_and_overflow(factory, monkeypatch):
    from repoharbor.persistence import validate_state

    with factory() as engine:
        saved = engine.store.data
        saved["discovery"] = {
            "state": "failed",
            "started_at": "test",
            "retry_at": 10**400,
        }
        with pytest.raises(HarborError):
            validate_state(saved)
        path = factory.root / "data.json"
        monkeypatch.setattr(json, "load", Mock(side_effect=RecursionError))
        with pytest.raises(HarborError):
            read_json(path)


def test_argparse_never_echoes_invalid_input(monkeypatch, capsys):
    token = "fictional-test-token"
    monkeypatch.setattr(sys, "argv", ["repoharbor", token])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert token not in capsys.readouterr().err


@pytest.mark.parametrize("entry", ["file", "unknown_instance", "root_link"])
def test_catalog_rejects_untrusted_directories(factory, tmp_path, entry):
    from repoharbor.catalog import catalog_entries

    root = factory.root
    if entry == "file":
        (root / "manual").mkdir()
        (root / "manual" / "demo").touch()
    elif entry == "unknown_instance":
        (root / "github" / "example.org").mkdir(parents=True)
    else:
        root = tmp_path / "linked-root"
        root.symlink_to(factory.root)
    with pytest.raises(HarborError):
        catalog_entries(root)
