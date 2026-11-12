import contextlib
import multiprocessing
import os
import signal
from dataclasses import replace

import pytest
from conftest import git

from repoharbor.errors import HarborError
from repoharbor.persistence import read_json
from repoharbor.recovery import recover, repair


@pytest.mark.parametrize("kind", ["clone", "update"])
@pytest.mark.parametrize("checkpoint", ["journal", "git", "promoted", "metadata"])
def test_sigkill_at_durable_boundaries(factory, remote, kind, checkpoint):
    if kind == "update":
        with factory() as engine:
            engine.run()
        git("update-ref", "refs/heads/new", "refs/heads/main", cwd=remote[1])
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()

    def crash_run():
        def pause(point):
            if point == checkpoint:
                child.send(point)
                child.recv()

        with factory(checkpoint=pause) as engine:
            engine.run()

    process = context.Process(target=crash_run)
    process.start()
    try:
        assert parent.poll(15), "checkpoint not reached"
        assert parent.recv() == checkpoint
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join()
    with factory() as engine:
        before = engine.repos["demo"]["backup"].copy()
        recover(engine)
        after = engine.repos["demo"]["backup"].copy()
        recover(engine)
        assert engine.repos["demo"]["backup"] == after  # idempotent
        if checkpoint in ("git", "promoted"):
            assert after["successful_updates"] == before["successful_updates"] + 1
            assert after["last_checked_at"] is not None
        else:
            assert after["successful_updates"] == before["successful_updates"]
            assert after["last_checked_at"] == before["last_checked_at"]
        if kind == "update" or checkpoint in ("git", "promoted", "metadata"):
            mirror = engine.path("demo") / "repository.git"
            assert engine.git.healthy(mirror)
        if checkpoint in ("git", "promoted", "metadata"):
            assert after["status"] == "updated"
        else:
            assert after["status"] == "interrupted"
        assert read_json(factory.root / "data.json")["recovery"]["last_checked_at"]


def test_orphan_partial_is_preserved_without_remote_action(factory):
    with factory() as engine:
        partial = engine.path("demo") / "repository.git.partial"
        partial.mkdir()
        (partial / "precious").write_text("preserve")
        recover(engine)
        engine.run()
        assert (partial / "precious").read_text() == "preserve"
        assert engine.git.calls == []
        assert engine.repos["demo"]["backup"]["status"] == "interrupted"


@pytest.mark.parametrize(
    "active",
    [{}, {"alias": "unknown", "type": "initial_clone", "id": "x", "state": "running"}],
)
def test_invalid_journal_fails_closed(factory, active):
    with factory() as engine:
        engine.store.data["active_operation"] = active
        with pytest.raises(HarborError):
            recover(engine)


def test_real_missing_object_repair_preserves_refs(factory, remote):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        old_refs = engine.git.refs(mirror)
        oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
        # Unlink the local hardlink; the remote object remains intact.
        (mirror / "objects" / oid[:2] / oid[2:]).unlink()
        assert not engine.git.healthy(mirror)
        engine.repos["demo"]["integrity"]["status"] = "suspect"
        engine.save_meta("demo")
        recover(engine)
        assert engine.git.healthy(mirror)
        assert engine.git.refs(mirror) == old_refs
        assert engine.repos["demo"]["integrity"]["successful_repairs"] == 1
        assert engine.repos["demo"]["integrity"]["status"] == "healthy"


@pytest.mark.parametrize(
    "mode", ["excluded", "disabled", "limited", "failure", "invalid"]
)
def test_repair_guards_and_three_attempts(factory, remote, mode, monkeypatch):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        before = engine.git.refs(mirror)
        engine.repos["demo"]["integrity"]["status"] = "corrupt"
        if mode == "excluded":
            engine.config = replace(engine.config, sources=())
        elif mode == "disabled":
            engine.config = replace(engine.config, automatic_repair=False)
        elif mode == "limited":
            monkeypatch.setattr(
                engine.budget, "status", lambda *a: "global_limit_paused"
            )
        elif mode == "failure":

            def fail(*a, **kw):
                raise HarborError("GIT_FAILED")

            monkeypatch.setattr(engine.git, "fetch", fail)
        else:
            monkeypatch.setattr(engine.git, "healthy", lambda *a: False)
        repair(engine, "demo")
        attempts = engine.repos["demo"]["integrity"]["repair_attempts"]
        assert attempts == (3 if mode in ("failure", "invalid") else 0)
        repair(engine, "demo")
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == attempts
        expected_status = "repair_paused" if mode == "limited" else "repair_failed"
        assert engine.repos["demo"]["backup"]["status"] == expected_status
        if mode == "limited":
            assert (
                engine.repos["demo"]["backup"]["repair_deferred"]
                == "global_limit_paused"
            )
        assert engine.git.refs(mirror) == before


def test_metadata_write_failure_does_not_complete_journal(factory, monkeypatch):
    with factory() as engine:
        original = engine.save_meta

        def fail_after_git(alias):
            if engine.repos[alias]["backup"]["completed_operation"]:
                raise OSError(28, "synthetic ENOSPC")
            original(alias)

        monkeypatch.setattr(engine, "save_meta", fail_after_git)
        with pytest.raises(OSError):
            engine.run()
        assert (
            read_json(factory.root / "data.json")["active_operation"]["state"]
            == "running"
        )
    with factory() as engine:
        recover(engine)
        assert engine.git.healthy(engine.path("demo") / "repository.git")


def test_missing_metadata_never_reclones(factory):
    with factory() as engine:
        engine.run()
        path = engine.path("demo") / "metadaten.json"
        path.unlink()
    with pytest.raises(HarborError), factory():
        pass


@pytest.mark.parametrize("kind", ["clone", "update"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_signal_while_git_command_active(factory, remote, kind, sig):
    """A real upload-pack process blocks at an IPC barrier during Git transfer."""
    import subprocess

    from conftest import LocalGit

    if kind == "update":
        with factory() as engine:
            engine.run()
        git("update-ref", "refs/heads/new", "refs/heads/main", cwd=remote[1])
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    # The upload-pack wrapper signals readiness through a separate inherited FD.
    read_fd, write_fd = os.pipe()
    wrapper = factory.root.parent / "blocked-upload-pack"
    wrapper.write_text(
        "#!/usr/bin/env python3\nimport os,signal\n"
        f"os.write({write_fd}, b'R')\nsignal.pause()\n"
    )
    wrapper.chmod(0o700)

    def child_run():
        active_pid = None
        real_popen = subprocess.Popen

        def popen(args, **kwargs):
            nonlocal active_pid
            if "clone" in args or "fetch" in args:
                # Only the test transport inherits the barrier FD.
                index = args.index("clone") if "clone" in args else args.index("fetch")
                args = [
                    *args[: index + 1],
                    f"--upload-pack={wrapper}",
                    *args[index + 1 :],
                ]
                kwargs["pass_fds"] = (write_fd,)
            process = real_popen(args, **kwargs)
            if "clone" in args or "fetch" in args:
                active_pid = process.pid
                child.send(active_pid)
            return process

        subprocess.Popen = popen
        git_instance = LocalGit(remote[1])
        signal.signal(signal.SIGTERM, lambda *_: git_instance.stop.set())
        with factory(git_instance=git_instance) as engine:
            engine.run()

    process = ctx.Process(target=child_run)
    process.start()
    git_pid = None
    try:
        assert parent.poll(15)
        git_pid = parent.recv()
        import select

        assert select.select([read_fd], [], [], 15)[0], "upload-pack not active"
        assert os.read(read_fd, 1) == b"R"
        os.kill(process.pid, sig)
        process.join(15)
        assert not process.is_alive()
        if sig == signal.SIGKILL:
            assert process.exitcode == -sig
        else:
            assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        if git_pid:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(git_pid, signal.SIGKILL)
        os.close(read_fd)
        os.close(write_fd)
    with factory() as engine:
        recover(engine)
        assert engine.repos["demo"]["backup"]["status"] in ("error", "interrupted")
        if kind == "update":
            assert engine.git.healthy(engine.path("demo") / "repository.git")
