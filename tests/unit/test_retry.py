import json
import os
import threading
from argparse import Namespace
from dataclasses import replace
from unittest.mock import Mock

import pytest

from repoharbor import status
from repoharbor.config import Retry, Source
from repoharbor.errors import GitError, HarborError, Result
from repoharbor.git_backup import Git, transient_git_error
from repoharbor.persistence import atomic_json
from repoharbor.recovery import recover


def fail(code="GIT_NETWORK", source_status="unavailable"):
    return Mock(side_effect=GitError(code, source_status))


@pytest.mark.parametrize(
    ("diagnostic", "code"),
    [
        (
            b"fatal: unable to access 'https://example.test/r': "
            b"failed to connect to host",
            "GIT_NETWORK",
        ),
        (b"error: rpc failed; curl 18 partial file", "GIT_NETWORK"),
        (b"error: rpc failed; curl 56 recv failure", "GIT_NETWORK"),
        (b"error: rpc failed; curl 28 operation timed out", "GIT_TIMEOUT"),
        (
            b"fatal: unable to access 'https://example.test/r': connection timed out",
            "GIT_TIMEOUT",
        ),
        (
            b"error: rpc failed; http 503 curl 22 server unavailable",
            "GIT_SERVER_TEMPORARY",
        ),
        (
            b"fatal: unable to access 'https://example.test/r': "
            b"the requested url returned error: 502",
            "GIT_SERVER_TEMPORARY",
        ),
        (b"error: rpc failed; http 403 curl 22 access denied", None),
        (
            b"fatal: unable to access 'https://example.test/r': "
            b"server certificate verification failed",
            None,
        ),
        (
            b"fatal: unable to access 'https://example.test/r': "
            b"could not resolve host: example.test",
            None,
        ),
        (b"remote: error: rpc failed; curl 56 recv failure", None),
        (b"fatal: early eof\nfatal: invalid index-pack output", None),
        (b"fatal: authentication failed", None),
        (b"fatal: repository not found", None),
        (b"error: rpc failed; http 429 curl 22 rate limit", None),
    ],
)
def test_only_specific_transport_evidence_can_retry(diagnostic, code):
    assert transient_git_error(diagnostic) == code


@pytest.mark.parametrize(
    ("active", "command", "interrupt", "expected"),
    [
        (True, ["ls-remote", "--", "https://example.test/r"], False, "GIT_TIMEOUT"),
        (False, ["ls-remote", "--", "https://example.test/r"], False, "INTERRUPTED"),
        (True, ["ls-remote", "--", "https://example.test/r"], True, "INTERRUPTED"),
        (
            True,
            ["-C", "repo", "fetch", "--refetch", "--", "https://example.test/r"],
            False,
            "INTERRUPTED",
        ),
        (True, ["--version"], False, "INTERRUPTED"),
    ],
)
def test_git_deadline_is_distinct_from_shutdown_and_local_commands(
    monkeypatch, active, command, interrupt, expected
):
    process = Mock(pid=123)
    process.poll.return_value = None
    for name in ("stdout", "stderr"):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        setattr(process, name, os.fdopen(read_fd, "rb"))
    popen = Mock(return_value=process)
    monkeypatch.setattr("repoharbor.git_backup.subprocess.Popen", popen)
    monkeypatch.setattr("repoharbor.git_backup.os.killpg", Mock())
    git = Git(Mock(git_env=Mock(return_value={})), threading.Event(), timeout=-1)
    git.classify_transient = active
    with pytest.raises(HarborError, match=expected):
        git.run(command, interrupt=lambda: interrupt)
    assert process.wait.called
    env = popen.call_args.kwargs["env"]
    if active and command[0] == "ls-remote":
        assert env["GIT_HTTP_MAX_RETRIES"] == "0"
        assert env["GIT_HTTP_MAX_RETRY_TIME"] == "-1"
    else:
        assert "GIT_HTTP_MAX_RETRIES" not in env


@pytest.mark.parametrize(
    ("enabled", "code", "expected"),
    [
        (False, "GIT_NETWORK", False),
        (True, "GIT_FAILED", False),
        (True, "ENOSPC", False),
    ],
)
def test_disabled_or_ambiguous_failure_does_not_schedule(
    factory, monkeypatch, enabled, code, expected
):
    with factory(replace(factory.config, auto_retry=enabled)) as engine:
        monkeypatch.setattr(engine.git, "clone", fail(code))
        assert engine.run() == Result.BACKUP_FAILED
        assert engine.repos["demo"]["source"]["status"] == "unavailable"
        assert ("retry" in engine.repos["demo"]) == expected
        assert engine.retries.due() is None


def test_no_retry_state_on_initial_success(factory):
    with factory(replace(factory.config, auto_retry=True)) as engine:
        assert engine.run() == Result.SUCCESS
        assert "retry" not in engine.repos["demo"]


def test_retry_backoff_limit_and_regular_success(factory, monkeypatch, caplog):
    caplog.set_level("INFO")
    cfg = replace(factory.config, auto_retry=True, retry=Retry(3, 5, 12))
    clock = Mock(return_value=1000)
    with factory(cfg) as engine:
        engine.retries.clock = clock
        clone = engine.git.clone
        monkeypatch.setattr(engine.git, "clone", fail())
        assert engine.run(manual=True) == Result.BACKUP_FAILED
        state = engine.repos["demo"]["retry"]
        assert state["next_attempt_at"] == 1005
        clock.return_value += 2
        assert engine.run() == Result.BACKUP_FAILED
        assert state["next_attempt_at"] == 1005
        assert engine.retries.due() is None
        for attempts, (due, next_due) in enumerate(
            [(1005, 1015), (1015, 1027), (1027, None)], 1
        ):
            clock.return_value = due
            assert engine.retries.due() == "demo"
            engine.retries.execute("demo")
            assert state["attempts"] == attempts
            assert state["last_attempt_at"] == due
            assert state["last_result"] == "failed"
            assert state["last_error_code"] == "GIT_NETWORK"
            assert state["next_attempt_at"] == next_due
        assert state["status"] == "exhausted"
        clock.return_value += 100000
        assert engine.retries.due() is None
        assert engine.run() == Result.BACKUP_FAILED
        assert state["status"] == "exhausted"
        monkeypatch.setattr(engine.git, "clone", clone)
        assert engine.run() == Result.SUCCESS
        meta = engine.repos["demo"]
        assert state["status"] == "succeeded" and state["attempts"] == 0
        assert state["last_result"] == "succeeded" and state["last_error_code"] is None
        assert meta["backup"]["successful_updates"] == 1
        assert meta["errors"]["total"] == 6 and meta["errors"]["consecutive"] == 0
        assert "retry limit reached repository=demo" in caplog.text


def test_regular_noop_success_cancels_retry(factory, monkeypatch):
    with factory(replace(factory.config, auto_retry=True)) as engine:
        assert engine.run() == Result.SUCCESS
        with monkeypatch.context() as patch:
            patch.setattr(engine.git, "remote_refs", fail())
            assert engine.run() == Result.BACKUP_FAILED
        assert engine.repos["demo"]["retry"]["status"] == "pending"
        assert engine.run(manual=True, repository="demo") == Result.SUCCESS
        meta = engine.repos["demo"]
        assert meta["retry"]["status"] == "succeeded"
        assert meta["retry"]["next_attempt_at"] is None
        assert meta["backup"]["successful_updates"] == 1


def test_other_repo_keeps_its_own_attempt_and_schedule(factory, monkeypatch):
    config = replace(
        factory.config,
        auto_retry=True,
        sources=(
            *factory.config.sources,
            Source("second", "https://other.example/r.git"),
        ),
    )
    with factory(config) as engine:
        clone = engine.git.clone
        clock = Mock(return_value=1000)
        engine.retries.clock = clock

        def clone_both(url, partial, *args):
            if partial.parent.name == "demo" and clock() == 1000:
                raise GitError("GIT_NETWORK")
            if partial.parent.name == "second" and clock() == 1000:
                clock.return_value = 1020
                raise GitError("GIT_TIMEOUT")
            clone(config.sources[0].url, partial, *args)

        monkeypatch.setattr(engine.git, "clone", clone_both)
        assert engine.run() == Result.BACKUP_FAILED
        clock.return_value = 1300
        assert engine.retries.due() == "demo"
        engine.retries.execute("demo")
        assert engine.repos["demo"]["retry"]["last_result"] == "succeeded"
        assert engine.repos["second"]["retry"]["attempts"] == 0
        clock.return_value = 1320
        assert engine.retries.due() == "second"
        engine.retries.execute("second")
        assert engine.repos["second"]["backup"]["successful_updates"] == 1


@pytest.fixture
def pending(factory, monkeypatch):
    config = replace(factory.config, auto_retry=True)
    with factory(config) as engine:
        engine.retries.clock = lambda: 1000
        with monkeypatch.context() as patch:
            patch.setattr(engine.git, "clone", fail())
            assert engine.run() == Result.BACKUP_FAILED
    return config


@pytest.mark.parametrize(
    ("safeguard", "reason"),
    [
        ("capacity", "global_limit_paused"),
        ("quarantine", "RECOVERY_REQUIRED"),
        ("attention", "RECOVERY_REQUIRED"),
        ("excluded", "NOT_SELECTED"),
        ("partial", "RECOVERY_REQUIRED"),
        ("repair_journal", "RECOVERY_REQUIRED"),
        ("missing_mirror", "RECOVERY_REQUIRED"),
    ],
)
def test_retry_preflight_blocks_without_remote_git(
    factory, pending, monkeypatch, safeguard, reason
):
    if safeguard == "excluded":
        pending = replace(pending, sources=())
    with factory(pending) as engine:
        engine.retries.clock = lambda: 1300
        meta = engine.repos["demo"]
        folder = engine.path("demo")
        if safeguard in ("partial", "quarantine"):
            (folder / "repository.git.partial").mkdir()
        if safeguard == "quarantine":
            (folder / "quarantine" / "initial-clone").mkdir(parents=True)
        if safeguard == "partial":
            recover(engine)
            # The owned partial is moved to the reserved slot; a second partial
            # has no room and must be preserved without another clone.
            (folder / "repository.git.partial").mkdir()
        if safeguard == "capacity":
            monkeypatch.setattr(engine, "refresh_storage", lambda alias: reason)
        if safeguard == "attention":
            meta["backup"]["manual_attention_required"] = "test"
        if safeguard == "repair_journal":
            recover(engine)
            engine.store.begin("demo", "repair")
        if safeguard == "missing_mirror":
            meta["backup"]["created_at"] = "2026-01-01T00:00:00Z"
        assert engine.retries.due() == "demo"
        engine.retries.execute("demo")
        state = meta["retry"]
        assert state["status"] == "blocked" and state["block_reason"] == reason
        assert state["attempts"] == 0
        assert not engine.git.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"attempts": True},
        {"attempts": 11},
        {"next_attempt_at": None},
        {"next_attempt_at": -1},
        {"next_attempt_at": 253402300800},
        {"reason": "raw data"},
        {"status": "idle"},
        {"last_attempt_at": 1500},
        {"last_result": "failed"},
        {"last_error_code": "GIT_FOUND_SECRET"},
        {"block_reason": []},
        {"discovery_deferrals": True},
        {"discovery_deferrals": -1},
        {"discovery_deferrals": 4},
        {"block_reason": "DISCOVERY_TEMPORARY"},
        {
            "status": "blocked",
            "next_attempt_at": None,
            "block_reason": "DISCOVERY_LIMIT_REACHED",
        },
        {"extra": "value"},
    ],
)
def test_invalid_retry_metadata_fails_closed(factory, pending, changes):
    path = factory.root / "manual" / "demo" / "metadaten.json"
    meta = json.loads(path.read_text())
    meta["retry"].update(changes)
    atomic_json(path, meta)
    before = path.read_bytes()
    with (
        pytest.raises(HarborError, match="Invalid repository metadata"),
        factory(pending),
    ):
        pytest.fail("invalid metadata accepted")
    assert path.read_bytes() == before


def test_pending_retry_status_json_and_text(factory, pending, capsys):
    config = pending
    args = Namespace(
        command="repositories", action="show", repository="demo", json=True
    )
    assert status.print_status(config, args) == Result.SUCCESS
    repo = json.loads(capsys.readouterr().out)
    assert repo["retry"] == {
        "status": "pending",
        "pending": True,
        "attempts": 0,
        "max_retries": 3,
        "next_attempt_at": "1970-01-01T00:21:40+00:00",
        "reason": "GIT_NETWORK",
        "last_attempt_at": None,
        "last_result": None,
        "last_error_code": None,
        "block_reason": None,
        "limit_reached": False,
        "discovery_deferred": False,
        "next_discovery_check_at": None,
        "discovery_deferrals": 0,
        "max_discovery_deferrals": 3,
    }
    for enabled in (True, False):
        args.command = "status"
        config = replace(config, auto_retry=enabled)
        assert status.print_status(config, args) == Result.SUCCESS
        assert json.loads(capsys.readouterr().out)["retry"] == {
            "enabled": enabled,
            "pending": int(enabled),
        }
    args.command, args.json = "repositories", False
    assert status.print_status(config, args) == Result.SUCCESS
    assert "reason  GIT_NETWORK" in capsys.readouterr().out


def test_disabled_pending_state_is_not_dispatched_after_restart(factory, pending):
    with factory(replace(pending, auto_retry=False)) as engine:
        engine.retries.clock = lambda: 2000
        recover(engine)
        assert engine.retries.due() is None
        assert engine.repos["demo"]["retry"]["status"] == "pending"
        assert not engine.git.calls


@pytest.mark.parametrize("provider_fixture", ["github_env", "gitlab_env"])
@pytest.mark.parametrize("discovery", ["fresh", "cooldown", "failed", "excluded"])
def test_provider_retry_refreshes_only_its_authority(
    request, monkeypatch, provider_fixture, discovery
):
    from repoharbor.config import Provider
    from repoharbor.github import DiscoveryError

    env = request.getfixturevalue(provider_fixture)
    other = Provider("other", "external", type="gitlab", host="unrelated.example:8443")
    config = replace(
        env.config, auto_retry=True, providers=(*env.config.providers, other)
    )
    if provider_fixture == "github_env":
        monkeypatch.setattr("repoharbor.engine.GitLab", lambda *a, **kw: env.client)
    with env.create(config, git_instance=env.git()) as engine:
        original_discover = env.client.discover.side_effect

        def scoped(provider):
            if provider == other:
                raise DiscoveryError("GITLAB_NETWORK")
            return original_discover(provider)

        env.client.discover.side_effect = scoped
        clock = Mock(return_value=1000)
        engine.retries.clock = clock
        clone = engine.git.clone
        monkeypatch.setattr(engine.git, "clone", fail())
        assert engine.run() == Result.BACKUP_FAILED
        assert engine.repos[env.key]["retry"]["status"] == "pending"
        monkeypatch.setattr(engine.git, "clone", Mock(side_effect=clone))
        env.client.discover.reset_mock()
        if discovery == "cooldown":
            data = engine.store.data
            provider, _, _ = env.key.rpartition(":")
            ledger = (
                data["discovery"]
                if provider.startswith("github:")
                else data["provider_discovery"][provider]
            )
            ledger["retry_at"] = 1600
        elif discovery == "failed":
            env.client.discover.side_effect = DiscoveryError("GITHUB_NETWORK")
        elif discovery == "excluded":
            env.results.update(first=[], second=[])
        clock.return_value = 1300
        assert engine.retries.due() == env.key
        engine.retries.execute(env.key)
        state = engine.repos[env.key]["retry"]
        assert all(call.args[0] != other for call in env.client.discover.call_args_list)
        if discovery == "fresh":
            assert state["last_result"] == "succeeded"
            engine.git.clone.assert_called_once()
            assert engine.repos[env.key]["backup"]["successful_updates"] == 1
        else:
            engine.git.clone.assert_not_called()
            assert state["attempts"] == 0
            assert (
                state["block_reason"]
                == {
                    "cooldown": "PROVIDER_COOLDOWN",
                    "failed": "DISCOVERY_FAILED",
                    "excluded": "NOT_SELECTED",
                }[discovery]
            )
            if discovery == "cooldown":
                env.client.discover.assert_not_called()
                assert state["next_attempt_at"] == 1600
                assert engine.retries.due() is None


@pytest.mark.parametrize("local_state", ["healthy", "corrupt", "locked", "unsupported"])
def test_retry_fetch_requires_offline_recovery_and_keeps_repair_limits(
    factory, remote, monkeypatch, local_state
):
    from conftest import git

    cfg = replace(factory.config, auto_retry=True)
    with factory(cfg) as engine:
        assert engine.run() == Result.SUCCESS
        git("update-ref", "refs/heads/new", "refs/heads/main", cwd=remote[1])
        engine.retries.clock = lambda: 1000
        normal_fetch = engine.git.fetch
        monkeypatch.setattr(engine.git, "fetch", fail())
        assert engine.run() == Result.BACKUP_FAILED
        assert engine.store.data["active_operation"]["state"] == "interrupted"
        assert engine.repos["demo"]["integrity"]["status"] == "suspect"
        meta = engine.repos["demo"]
        meta["integrity"]["repair_attempts"] = 2
        mirror = engine.path("demo") / "repository.git"
        if local_state == "corrupt":
            oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
            (mirror / "objects" / oid[:2] / oid[2:]).unlink()
        elif local_state == "locked":
            (mirror / "HEAD.lock").touch()
        elif local_state == "unsupported":
            git("config", "fsck.missingEmail", "ignore", cwd=mirror)

        def fetch_after_recovery(*args):
            assert meta["integrity"]["status"] == "healthy"
            assert meta["integrity"]["repair_attempts"] == 2
            assert engine.store.data["active_operation"]["type"] == "mirror_update"
            assert engine.store.data["active_operation"]["state"] == "running"
            return normal_fetch(*args)

        fetch = Mock(side_effect=fetch_after_recovery)
        monkeypatch.setattr(engine.git, "fetch", fetch)
        engine.retries.clock = lambda: 1300
        engine.retries.execute("demo")
        if local_state == "healthy":
            fetch.assert_called_once()
            assert meta["backup"]["successful_updates"] == 2
            assert meta["retry"]["last_result"] == "succeeded"
        else:
            fetch.assert_not_called()
            assert meta["backup"]["successful_updates"] == 1
            assert meta["retry"]["block_reason"] == "RECOVERY_REQUIRED"
            assert engine.store.data["active_operation"]["state"] == (
                "interrupted" if local_state == "unsupported" else "reconciled"
            )
        assert meta["integrity"]["repair_attempts"] == 2
