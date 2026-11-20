import multiprocessing
import signal
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from repoharbor.errors import GitError, HarborError, Result
from repoharbor.recovery import recover


@pytest.mark.parametrize(
    "boundary",
    ["retry_planned", "retry_claimed", "journal", "git", "promoted", "metadata"],
)
def test_kill_on_retry_boundaries(factory, boundary):
    config = replace(factory.config, auto_retry=True)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()

    def crash():
        def pause(point):
            if point == boundary:
                child.send(point)
                child.recv()

        with factory(config) as engine:
            clock = Mock(return_value=1000)
            engine.retries.clock = clock
            clone = engine.git.clone
            engine.git.clone = Mock(side_effect=GitError("GIT_NETWORK"))
            if boundary == "retry_planned":
                engine.checkpoint = pause
            assert engine.run() == Result.BACKUP_FAILED
            engine.git.clone = clone
            engine.checkpoint = pause
            clock.return_value = 1300
            engine.retries.execute("demo")

    process = context.Process(target=crash)
    process.start()
    try:
        assert parent.poll(15), "checkpoint not reached"
        assert parent.recv() == boundary
        process.kill()
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()

    with factory(config) as engine:
        engine.retries.clock = lambda: 10000
        recover(engine)
        meta = engine.repos["demo"]
        if boundary == "retry_planned":
            assert engine.retries.due() == "demo"
            engine.retries.execute("demo")
            expected = "succeeded"
        elif boundary in ("retry_claimed", "journal"):
            assert meta["retry"]["attempts"] == 1
            assert meta["retry"]["last_result"] == "interrupted"
            expected = "blocked"
        else:
            expected = "succeeded"
        assert engine.retries.due() is None
        assert meta["retry"]["status"] == expected
        assert meta["backup"]["successful_updates"] == int(expected == "succeeded")
        assert meta["errors"]["total"] == 1
        if expected == "succeeded":
            assert meta["retry"]["attempts"] == 0
            assert meta["retry"]["last_result"] == "succeeded"
        before = meta["retry"].copy()
        recover(engine)
        assert meta["retry"] == before
        assert meta["backup"]["successful_updates"] == int(expected == "succeeded")


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGKILL])
def test_signal_during_retry_preserves_partial_and_does_not_replay(
    factory, stop_signal
):
    config = replace(factory.config, auto_retry=True)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()

    def retry_with_partial():
        with factory(config) as engine:
            signal.signal(signal.SIGTERM, lambda *_: engine.git.stop.set())
            engine.retries.clock = lambda: 1000
            engine.git.clone = Mock(side_effect=GitError("GIT_NETWORK"))
            assert engine.run() == Result.BACKUP_FAILED

            def partially_clone(url, partial, *args):
                partial.mkdir()
                (partial / "evidence").write_text("preserved")
                child.send("git executing")
                deadline = time.monotonic() + 10
                while not engine.git.stop.is_set():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                raise HarborError("INTERRUPTED")

            engine.git.clone = partially_clone
            engine.retries.clock = lambda: 1300
            engine.retries.execute("demo")

    process = context.Process(target=retry_with_partial)
    process.start()
    try:
        assert parent.poll(15)
        assert parent.recv() == "git executing"
        if stop_signal == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
        process.join(10)
        assert process.exitcode == (
            -stop_signal if stop_signal == signal.SIGKILL else 0
        )
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()

    with factory(config) as engine:
        engine.retries.clock = lambda: 20000
        recover(engine)
        folder = engine.path("demo")
        assert (
            folder / "quarantine" / "initial-clone" / "evidence"
        ).read_text() == "preserved"
        assert not (folder / "repository.git").exists()
        assert engine.retries.due() is None
        meta = engine.repos["demo"]
        assert meta["retry"]["status"] == "blocked"
        assert meta["retry"]["last_result"] == "interrupted"
        assert meta["errors"]["total"] == 1
        assert not engine.git.calls


def test_failure_writing_retry_claim_prevents_git_and_can_retry_after_restart(
    factory, monkeypatch
):
    import errno

    from repoharbor.engine import atomic_json

    config = replace(factory.config, auto_retry=True)
    with factory(config) as engine:
        engine.retries.clock = lambda: 1000
        with monkeypatch.context() as patch:
            patch.setattr(
                engine.git, "clone", Mock(side_effect=GitError("GIT_NETWORK"))
            )
            assert engine.run() == Result.BACKUP_FAILED

        def fail_claim(path, value):
            if value.get("retry", {}).get("status") == "running":
                raise OSError(errno.EIO, "fault injection")
            atomic_json(path, value)

        engine.retries.clock = lambda: 1300
        with monkeypatch.context() as patch:
            patch.setattr("repoharbor.engine.atomic_json", fail_claim)
            with pytest.raises(OSError, match="fault injection"):
                engine.retries.execute("demo")
        assert not engine.git.calls
        assert engine.store.data["active_operation"]["state"] == "reconciled"
    with factory(config) as engine:
        engine.retries.clock = lambda: 1500
        recover(engine)
        assert engine.retries.due() == "demo"
        engine.retries.execute("demo")
        meta = engine.repos["demo"]
        assert meta["retry"]["last_result"] == "succeeded"
        assert meta["backup"]["successful_updates"] == 1


def test_interrupted_retry_fetch_cannot_start_remote_repair_on_restart(
    factory, remote, monkeypatch
):
    from conftest import git

    config = replace(factory.config, auto_retry=True)
    with factory(config) as engine:
        assert engine.run() == Result.SUCCESS
        git("update-ref", "refs/heads/new", "refs/heads/main", cwd=remote[1])
        engine.retries.clock = lambda: 1000
        monkeypatch.setattr(
            engine.git, "remote_refs", Mock(side_effect=GitError("GIT_NETWORK"))
        )
        assert engine.run() == Result.BACKUP_FAILED
        monkeypatch.undo()

        def interrupt_fetch(mirror, *args):
            oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
            (mirror / "objects" / oid[:2] / oid[2:]).unlink()
            engine.git.stop.set()
            raise HarborError("INTERRUPTED")

        monkeypatch.setattr(engine.git, "fetch", interrupt_fetch)
        engine.retries.clock = lambda: 1300
        engine.retries.execute("demo")
        assert engine.repos["demo"]["retry"]["status"] == "blocked"
        assert engine.store.data["active_operation"]["trigger"] == "retry"
    with factory(config) as engine:
        unexpected_fetch = Mock(side_effect=AssertionError("remote repair on startup"))
        monkeypatch.setattr(engine.git, "fetch", unexpected_fetch)
        recover(engine)
        unexpected_fetch.assert_not_called()
        meta = engine.repos["demo"]
        assert meta["integrity"]["status"] == "corrupt"
        assert meta["integrity"]["repair_attempts"] == 0
        assert meta["backup"]["repair_deferred"] == "RECOVERY_REQUIRED"
        assert meta["backup"]["successful_updates"] == 1
        assert engine.retries.due() is None
        assert engine.store.data["active_operation"]["state"] == "reconciled"


@pytest.mark.parametrize(
    "phase", ["before_retry_discovery_deferral", "retry_discovery_deferred"]
)
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_discovery_deferral_survives_signals(github_env, phase, signum):
    from repoharbor.github import DiscoveryError

    env = github_env
    config = replace(env.config, auto_retry=True)
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()

    def interrupted():
        with env.create(config, git_instance=env.git()) as engine:
            signal.signal(signal.SIGTERM, lambda *_: engine.git.stop.set())
            engine.retries.clock = lambda: 1000
            original_clone = engine.git.clone
            engine.git.clone = Mock(side_effect=GitError("GIT_NETWORK"))
            assert engine.run(repository=env.key) == Result.BACKUP_FAILED
            engine.git.clone = Mock(wraps=original_clone)
            env.client.discover.side_effect = DiscoveryError(
                "GITHUB_HTTP_ERROR", temporary=True
            )

            def checkpoint(point):
                if point == phase:
                    child.send(point)
                    deadline = time.monotonic() + 10
                    while not engine.git.stop.is_set():
                        assert time.monotonic() < deadline
                        time.sleep(0.01)

            engine.checkpoint = checkpoint
            engine.retries.clock = lambda: 1300
            engine.retries.execute(env.key)
            engine.git.clone.assert_not_called()

    process = ctx.Process(target=interrupted)
    process.start()
    try:
        assert parent.poll(15)
        assert parent.recv() == phase
        if signum == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
        process.join(15)
        assert process.exitcode == (-signum if signum == signal.SIGKILL else 0)
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()

    with env.create(config, git_instance=env.git()) as engine:
        recover(engine)
        meta = engine.repos[env.key]
        retry = meta["retry"]
        saved = int(signum == signal.SIGTERM or phase == "retry_discovery_deferred")
        assert retry.get("discovery_deferrals", 0) == saved
        assert retry["next_attempt_at"] == (1600 if saved else 1300)
        assert retry["attempts"] == meta["backup"]["successful_updates"] == 0
        engine.retries.clock = lambda: 9000
        assert engine.retries.due() == env.key
        engine.retries.execute(env.key)
        assert engine.retries.due() is None
        recover(engine)
        assert engine.repos[env.key]["backup"]["successful_updates"] == 1
        assert engine.repos[env.key]["retry"]["last_result"] == "succeeded"
