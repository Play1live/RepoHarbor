import importlib
import runpy
import signal
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import LocalCredentials

from repoharbor import __version__, askpass, cli
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError
from repoharbor.git_backup import Git


def test_askpass(monkeypatch, capsys):
    monkeypatch.setenv("REPOHARBOR_ASKPASS_TOKEN", "fictional-test-token")
    monkeypatch.setattr(sys, "argv", ["askpass", "Username for https://example.org"])
    askpass.main()
    assert capsys.readouterr().out == "oauth2\n"
    monkeypatch.setattr(sys, "argv", ["askpass", "Password"])
    askpass.main()
    assert capsys.readouterr().out == "fictional-test-token\n"
    with pytest.raises(SystemExit) as exit_info:
        monkeypatch.setattr(sys, "argv", ["repoharbor", "--version"])
        runpy.run_module("repoharbor", run_name="__main__")
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["askpass", "Username"])
    runpy.run_path(askpass.__file__, run_name="__main__")
    assert capsys.readouterr().out == "oauth2\n"


def test_cli_validation_and_startup(factory, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load", lambda _: factory.config)
    monkeypatch.setattr(sys, "argv", ["repoharbor", "config", "validate"])
    assert cli.main() == 0
    assert capsys.readouterr().out == "Configuration valid\n"
    monkeypatch.setattr(sys, "argv", ["repoharbor", "config"])
    assert cli.main() == 1
    monkeypatch.setattr(sys, "argv", ["repoharbor"])
    monkeypatch.setattr(cli, "verify_mount", lambda _: None)
    stopped = []

    def serve(engine, stop):
        signal.raise_signal(signal.SIGTERM)
        assert stop.is_set()
        stopped.append(True)

    monkeypatch.setattr(cli, "serve", serve)
    assert cli.main() == 0 and stopped == [True]
    monkeypatch.setattr(cli, "verify_mount", Mock(side_effect=OSError("private-error")))
    assert cli.main() == 1
    monkeypatch.setattr(cli, "load", Mock(side_effect=HarborError("invalid setting")))
    assert cli.main() == 1


def test_mount_validation(tmp_path, monkeypatch):
    original = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: True if str(p) == "/.dockerenv" else original(p)
    )
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("1 2 3:4 / /backups rw - ext4 /dev/sda rw\n")
    cli.verify_mount(Path("/backups"), mountinfo)
    with pytest.raises(HarborError):
        cli.verify_mount(Path("/other"), mountinfo)
    monkeypatch.setattr(
        Path, "exists", lambda p: False if str(p) == "/.dockerenv" else original(p)
    )
    cli.verify_mount(Path("/other"), mountinfo)


def test_service_observes_ticks_and_propagates_worker_failure(factory, monkeypatch):
    with factory() as engine:
        for fail in (False, True):
            stop = threading.Event()

            class Clock:
                count = 0

                @classmethod
                def now(cls, zone):
                    cls.count += 1
                    return datetime(2026, 1, 1, 3, min(cls.count, 3), tzinfo=UTC)

            engine.config = replace(engine.config, schedule="* * * * *")

            def run(stop=stop, fail=fail):
                stop.set()
                if fail:
                    raise OSError("synthetic")

            monkeypatch.setattr(cli, "datetime", Clock)
            monkeypatch.setattr(engine, "run", run)
            if fail:
                with pytest.raises(HarborError, match="worker"):
                    cli.serve(engine, stop)
            else:
                cli.serve(engine, stop)


def test_git_security_failed_protocols_and_invalid_mirrors(tmp_path):
    git = Git(Credentials({}), threading.Event())
    with pytest.raises(HarborError, match="GIT_FAILED"):
        git.remote_refs("file:///tmp/not-allowed", None)
    assert not git.healthy(tmp_path)
    git.run(["init", tmp_path / "work"])
    assert not git.healthy(tmp_path / "work")
    git.stop.set()
    with pytest.raises(HarborError, match="INTERRUPTED"):
        git.run(["--version"])
    git.stop.clear()
    with pytest.raises(HarborError, match="INVALID_MIRROR"):
        # An instrumented format failure after an actual clone.
        git.healthy = lambda _: False
        local = Git(LocalCredentials({}), threading.Event())
        git.credentials = local.credentials
        git.clone(str(tmp_path / "work"), tmp_path / "partial", None, lambda: False)


@pytest.mark.parametrize("kill_timeout", [False, True])
def test_git_interrupt_kills_and_reaps_process_tree(monkeypatch, kill_timeout):
    import subprocess

    module = importlib.import_module("repoharbor.git_backup")
    process = Mock()
    process.pid = 123
    process.poll.return_value = None
    if kill_timeout:
        process.wait.side_effect = [subprocess.TimeoutExpired("git", 5), 0]
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    git = Git(Credentials({}), threading.Event(), timeout=-1)
    with pytest.raises(HarborError, match="INTERRUPTED"):
        git.run(["--version"])
    assert kill.call_args_list[0].args == (123, signal.SIGTERM)
    if kill_timeout:
        assert kill.call_args_list[1].args == (123, signal.SIGKILL)
    assert process.wait.called
