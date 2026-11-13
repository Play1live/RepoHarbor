import contextlib
import errno
import multiprocessing
import os
import select
import signal
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from conftest import LocalGit, git

from repoharbor.errors import HarborError
from repoharbor.persistence import read_json
from repoharbor.recovery import recover, repair
from repoharbor.scheduler import Cron, Scheduler
from repoharbor.storage import usage


def kill_at(factory, action, point):
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()

    def run():
        def pause(checkpoint):
            if checkpoint == point:
                child.send(True)
                child.recv()

        with factory(checkpoint=pause) as engine:
            action(engine)

    process = ctx.Process(target=run)
    process.start()
    try:
        assert parent.poll(15), f"checkpoint not reached: {point}"
        assert parent.recv()
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()
        child.close()


def seed_partial(factory):
    with factory() as engine:
        engine.store.begin("demo", "initial_clone")
        partial = engine.path("demo") / "repository.git.partial"
        partial.mkdir()
        (partial / "precious").write_text("preserved interruption")
    return partial


def corrupt(factory):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        refs = engine.git.refs(mirror)
        oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
        (mirror / "objects" / oid[:2] / oid[2:]).unlink()
        engine.repos["demo"]["integrity"]["status"] = "suspect"
        engine.save_meta("demo")
    return mirror, refs


@pytest.mark.parametrize(
    "point", ["quarantine_journal", "quarantine_renamed", "quarantine_metadata"]
)
def test_quarantine_sigkill_replay_and_scheduled_retry(factory, tmp_path, point):
    partial = seed_partial(factory)
    kill_at(factory, recover, point)
    with factory() as engine:
        recover(engine)
        archive = engine.path("demo") / "quarantine/initial-clone"
        assert (archive / "precious").read_text() == "preserved interruption"
        assert not partial.exists()
        assert not (engine.path("demo") / "repository.git").exists()
        snapshot = engine.repos["demo"]["backup"].copy()
        for _ in range(3):
            recover(engine)
            assert engine.repos["demo"]["backup"] == snapshot
        assert engine.repos["demo"]["errors"]["total"] == 0
        assert engine.git.calls == []
        engine.run()  # The future scheduled run, not boot recovery.
        mirror = engine.path("demo") / "repository.git"
        git("clone", "--no-hardlinks", mirror, tmp_path / "restore")
        assert (tmp_path / "restore/hello.txt").read_text() == "first\n"
        assert (archive / "precious").exists()


def test_quarantine_is_bounded_and_second_partial_is_preserved(factory, monkeypatch):
    seed_partial(factory)
    with factory() as engine:
        recover(engine)

        def fail_clone(url, partial, *args):
            partial.mkdir()
            (partial / "second").write_text("second interruption")
            raise HarborError("GIT_FAILED")

        monkeypatch.setattr(engine.git, "clone", fail_clone)
        engine.run()
        for _ in range(3):
            recover(engine)
            engine.run()
        folder = engine.path("demo")
        assert (folder / "repository.git.partial/second").exists()
        assert (folder / "quarantine/initial-clone/precious").exists()
        assert len(list((folder / "quarantine").iterdir())) == 1
        assert engine.repos["demo"]["backup"]["manual_attention_required"]
        assert engine.repos["demo"]["errors"]["total"] == 1


@pytest.mark.parametrize("excluded", [False, True])
def test_unowned_or_excluded_partial_requires_attention(factory, excluded):
    if excluded:
        partial = seed_partial(factory)
        cfg = replace(factory.config, sources=())
    else:
        cfg = factory.config
        with factory() as engine:
            partial = engine.path("demo") / "repository.git.partial"
            partial.mkdir()
            (partial / "precious").write_text("unknown")
    for _ in range(2):
        with factory(cfg) as engine:
            recover(engine)
            engine.run()
            assert partial.exists()
            assert not (engine.path("demo") / "quarantine").exists()
            assert engine.repos["demo"]["backup"]["manual_attention_required"]
            assert engine.repos["demo"]["errors"]["total"] == 0
            assert engine.git.calls == []


def test_quarantine_counts_towards_limits_and_resume(factory, tmp_path):
    partial = seed_partial(factory)
    (partial / "large").write_bytes(b"x" * 2_000_000)
    with factory() as engine:
        recover(engine)
        cfg = engine.budget.config
        engine.budget.config = replace(cfg, global_bytes=1_000_000)
        engine.run()
        assert engine.store.data["global_storage"]["status"] == "global_limit_paused"
        assert usage(factory.root) > 2_000_000
        assert engine.git.calls == []
        assert engine.repos["demo"]["backup"]["last_checked_at"] is None
        # Simulate an explicit operator archival action; application deletes nothing.
        (engine.path("demo") / "quarantine/initial-clone").rename(
            tmp_path / "operator-archive"
        )
        engine.run()
        assert engine.repos["demo"]["backup"]["successful_updates"] == 1
        assert (tmp_path / "operator-archive/large").exists()


@pytest.mark.parametrize("phase", ["journal", "rename", "directory_fsync"])
def test_quarantine_write_errors_preserve_and_recover(factory, monkeypatch, phase):
    partial = seed_partial(factory)
    with factory() as engine:
        with monkeypatch.context() as local:
            if phase == "journal":
                local.setattr(
                    engine.store,
                    "update_operation",
                    Mock(side_effect=OSError(errno.EIO, "synthetic")),
                )
            elif phase == "rename":
                local.setattr(
                    "repoharbor.recovery.os.rename",
                    Mock(side_effect=OSError(errno.EACCES, "synthetic")),
                )
            else:
                import repoharbor.recovery as recovery

                original = recovery.sync_directory
                count = [0]

                def fail(path):
                    count[0] += 1
                    if count[0] == 2:
                        raise OSError(errno.EIO, "synthetic")
                    original(path)

                local.setattr(recovery, "sync_directory", fail)
            with pytest.raises(OSError):
                recover(engine)
        assert (
            partial.exists()
            or (engine.path("demo") / "quarantine/initial-clone").exists()
        )
    with factory() as engine:
        recover(engine)
        assert (engine.path("demo") / "quarantine/initial-clone/precious").exists()
        assert engine.repos["demo"]["errors"]["total"] == 0


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize("kind", ["clone", "repair"])
def test_signal_during_real_git_mutation_preserves_budget(factory, remote, sig, kind):
    if kind == "repair":
        mirror, refs = corrupt(factory)
    else:
        mirror, refs = factory.root / "manual/demo/repository.git", None
    command = "fetch" if kind == "repair" else "clone"
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    read_fd, write_fd = os.pipe()
    wrapper = factory.root.parent / "repair-upload-pack"
    wrapper.write_text(
        "#!/usr/bin/env python3\nimport os,signal\n"
        f"os.write({write_fd}, b'R')\nsignal.pause()\n"
    )
    wrapper.chmod(0o700)

    def run():
        real_popen = subprocess.Popen

        def popen(args, **kwargs):
            if command in args:
                index = args.index(command)
                args = [
                    *args[: index + 1],
                    f"--upload-pack={wrapper}",
                    *args[index + 1 :],
                ]
                kwargs["pass_fds"] = (write_fd,)
            process = real_popen(args, **kwargs)
            if command in args:
                child.send(process.pid)
            return process

        subprocess.Popen = popen
        git_instance = LocalGit(remote[1])
        signal.signal(signal.SIGTERM, lambda *_: git_instance.stop.set())
        with factory(git_instance=git_instance) as engine:
            if kind == "repair":
                repair(engine, "demo")
            else:
                engine.run()

    process = ctx.Process(target=run)
    process.start()
    git_pid = None
    try:
        assert parent.poll(15)
        git_pid = parent.recv()
        assert select.select([read_fd], [], [], 15)[0]
        assert os.read(read_fd, 1) == b"R"
        os.kill(process.pid, sig)
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == (0 if sig == signal.SIGTERM else -signal.SIGKILL)
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        if git_pid:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(git_pid, signal.SIGKILL)
        os.close(read_fd)
        os.close(write_fd)
        parent.close()
        child.close()
    with factory() as engine:
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 0
        assert engine.repos["demo"]["errors"]["total"] == 0
        recover(engine)
        if kind == "clone":
            assert not mirror.exists()  # Boot never performs a replacement clone.
            engine.run()
        assert engine.git.healthy(mirror)
        if refs is not None:
            assert engine.git.refs(mirror) == refs
        repairs = 1 if kind == "repair" else 0
        assert engine.repos["demo"]["integrity"]["successful_repairs"] == repairs
        recover(engine)
        assert engine.repos["demo"]["integrity"]["successful_repairs"] == repairs


@pytest.mark.parametrize("point", ["repair_git", "repair_metadata"])
def test_repair_completion_is_idempotent_after_sigkill(factory, point):
    mirror, refs = corrupt(factory)
    kill_at(factory, lambda e: repair(e, "demo"), point)
    for _ in range(2):
        with factory() as engine:
            recover(engine)
            assert engine.git.healthy(mirror)
            assert engine.git.refs(mirror) == refs
            assert engine.repos["demo"]["integrity"]["successful_repairs"] == 1
            assert engine.repos["demo"]["errors"]["total"] == 0


def test_real_failures_bounded_across_restart_and_exclusion(factory, monkeypatch):
    mirror, refs = corrupt(factory)
    with factory() as engine:
        fetch = Mock(side_effect=HarborError("GIT_FAILED"))
        monkeypatch.setattr(engine.git, "fetch", fetch)
        recover(engine)
        assert fetch.call_count == 3
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 3
    with factory() as engine:
        fetch = Mock(side_effect=AssertionError("No more repair attempts"))
        monkeypatch.setattr(engine.git, "fetch", fetch)
        recover(engine)
        repair(engine, "demo")
        fetch.assert_not_called()
        assert engine.repos["demo"]["errors"]["total"] == 3
        assert engine.repos["demo"]["backup"]["status"] == "repair_failed"
        assert engine.git.refs(mirror) == refs
    with factory(replace(factory.config, sources=())) as engine:
        engine.repos["demo"]["integrity"]["status"] = "suspect"
        engine.git.calls.clear()
        recover(engine)
        assert not any("fetch" in args for args in engine.git.calls)
        assert engine.git.refs(mirror) == refs


def test_overlap_survives_sigkill_without_backfill(factory):
    def action(engine):
        engine.store.begin("demo", "initial_clone")
        scheduler = Scheduler(
            Cron("* * * * *"),
            "UTC",
            datetime(2026, 11, 12, tzinfo=UTC),
            on_skip=engine.update_skips,
        )
        scheduler.busy.acquire()
        scheduler.tick(datetime(2026, 11, 12, 0, 1, tzinfo=UTC), engine.run)
        engine.checkpoint("overlap_durable")

    kill_at(factory, action, "overlap_durable")
    with factory() as engine:
        assert engine.store.data["scheduler"]["skipped_overlaps"] == 1
        recover(engine)
        assert engine.git.calls == []
        assert engine.repos["demo"]["backup"]["last_checked_at"] is None
        assert engine.store.data["scheduler"]["skipped_overlaps"] == 1


@pytest.mark.parametrize("conflict", ["both", "missing", "parent_file"])
def test_pending_quarantine_conflict_requires_attention(factory, conflict):
    partial = seed_partial(factory)
    with factory() as engine:
        backup = engine.repos["demo"]["backup"]
        backup["quarantine"] = {
            "state": "pending",
            "operation_id": engine.store.data["active_operation"]["id"],
            "relative_path": "quarantine/initial-clone",
        }
        if conflict == "both":
            archive = engine.path("demo") / "quarantine/initial-clone"
            archive.mkdir(parents=True)
            (archive / "other").write_text("never overwrite")
        elif conflict == "missing":
            # A missing archive must remain visible after an operator move.
            partial.rename(factory.root.parent / "operator-preserved")
        else:
            (engine.path("demo") / "quarantine").write_text("unknown file")
        engine.save_meta("demo")
        recover(engine)
        assert engine.repos["demo"]["backup"]["manual_attention_required"]
        assert engine.git.calls == []
        if conflict != "missing":
            assert (partial / "precious").exists()


def test_corrupt_partial_with_completion_marker_is_not_promoted(factory):
    seed_partial(factory)
    with factory() as engine:
        engine.store.update_operation(
            git_completed_at="2026-11-12T00:00:00Z", changed=True
        )
        recover(engine)
        assert not (engine.path("demo") / "repository.git").exists()
        assert (engine.path("demo") / "quarantine/initial-clone/precious").exists()
        assert engine.repos["demo"]["backup"]["successful_updates"] == 0
        assert engine.repos["demo"]["backup"]["last_checked_at"] is None
        assert not any("clone" in args or "fetch" in args for args in engine.git.calls)


def test_repair_failure_checkpoint_is_not_counted_twice(factory):
    mirror, refs = corrupt(factory)

    def fail_once(engine):
        engine.git.fetch = Mock(side_effect=HarborError("GIT_FAILED"))
        repair(engine, "demo")

    kill_at(factory, fail_once, "repair_metadata")
    with factory() as engine:
        assert engine.repos["demo"]["errors"]["total"] == 1
        recover(engine)
        assert engine.git.healthy(mirror)
        assert engine.git.refs(mirror) == refs
        assert engine.repos["demo"]["errors"]["total"] == 1
        assert engine.repos["demo"]["integrity"]["successful_repairs"] == 1


def test_partial_ownership_survives_next_repository_journal(factory, monkeypatch):
    from repoharbor.config import Source

    config = replace(
        factory.config,
        sources=(
            *factory.config.sources,
            Source("other", "https://example.org/other.git"),
        ),
    )
    with factory(config) as engine:

        def fail(url, partial, *args):
            partial.mkdir()
            (partial / "precious").write_text("first source data")
            raise HarborError("GIT_FAILED")

        monkeypatch.setattr(engine.git, "clone", fail)
        engine.backup(config.sources[0])
        engine.store.begin("other", "initial_clone")
    with factory(config) as engine:
        recover(engine)
        assert (engine.path("demo") / "quarantine/initial-clone/precious").exists()
        assert engine.repos["demo"]["errors"]["total"] == 1
        assert engine.git.calls == []


def test_unknown_partial_never_replaces_valid_mirror_and_operator_can_resolve(
    factory, tmp_path
):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        before = engine.git.refs(mirror)
        inode = mirror.stat().st_ino
        partial = engine.path("demo") / "repository.git.partial"
        partial.mkdir()
        (partial / "unknown").write_text("preserve")
        recover(engine)
        assert engine.repos["demo"]["backup"]["manual_attention_required"]
        assert mirror.stat().st_ino == inode
        assert engine.git.refs(mirror) == before
        partial.rename(tmp_path / "operator-preserved")
        engine.run()
        assert "manual_attention_required" not in engine.repos["demo"]["backup"]
        assert mirror.stat().st_ino == inode
        assert engine.repos["demo"]["backup"]["status"] == "up_to_date"
        assert (tmp_path / "operator-preserved/unknown").exists()


def test_active_update_is_reconciled_before_other_source_quarantine(factory, remote):
    from repoharbor.config import Source

    class TwoRemotes(LocalGit):
        def run(self, args, credential=None, interrupt=lambda: False):
            args = [
                "https://example.org/repo.git"
                if arg == "https://example.org/other.git"
                else arg
                for arg in args
            ]
            return super().run(args, credential, interrupt)

    config = replace(
        factory.config,
        sources=(
            *factory.config.sources,
            Source("other", "https://example.org/other.git"),
        ),
    )
    with factory(config, git_instance=TwoRemotes(remote[1])) as engine:
        engine.backup(config.sources[1])
        old_time = engine.repos["other"]["backup"]["last_updated_at"]
        owner = engine.store.begin("demo", "initial_clone")
        engine.repos["demo"]["backup"]["partial_operation"] = owner
        engine.save_meta("demo")
        partial = engine.path("demo") / "repository.git.partial"
        partial.mkdir()
        (partial / "precious").write_text("retain")
        engine.store.begin("other", "mirror_update")

    # Reuse the IPC checkpoint helper with the same configuration.
    def configured_factory(checkpoint=None):
        return factory(config, checkpoint=checkpoint)

    kill_at(configured_factory, recover, "quarantine_journal")
    with factory(config) as engine:
        assert engine.repos["other"]["backup"]["status"] == "interrupted"
        assert engine.repos["other"]["backup"]["last_updated_at"] == old_time
        recover(engine)
        assert (engine.path("demo") / "quarantine/initial-clone/precious").exists()
        assert engine.repos["other"]["backup"]["successful_updates"] == 1
        assert not any("fetch" in args or "clone" in args for args in engine.git.calls)


@pytest.mark.parametrize(
    "point", ["repair_recovery_metadata", "repair_recovery_journal", "repair_git"]
)
def test_paused_repair_journal_handoff_survives_sigkill(factory, tmp_path, point):
    from repoharbor.config import Source

    config = replace(
        factory.config,
        sources=(
            *factory.config.sources,
            Source("other", "https://example.org/other.git"),
        ),
    )
    with factory(config) as engine:
        for alias in engine.repos:
            mirror = engine.path(alias) / "repository.git"
            engine.git.clone(config.sources[0].url, mirror, None, lambda: False)
            oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
            (mirror / "objects" / oid[:2] / oid[2:]).unlink()
            engine.repos[alias]["integrity"]["status"] = "suspect"
            engine.save_meta(alias)
        engine.store.begin("demo", "repair")
        engine.store.update_operation(state="interrupted")
        burden = engine.path("demo") / "preserved-burden"
        limit = usage(engine.path("demo")) + 16384
        burden.write_bytes(b"x" * 65536)
    config = replace(config, storage=replace(config.storage, overrides={"demo": limit}))

    def configured_factory(checkpoint=None):
        return factory(config, checkpoint=checkpoint)

    def recovery(engine):
        original = engine.git.fetch

        def fetch(mirror, url, *args, **kwargs):
            return original(mirror, config.sources[0].url, *args, **kwargs)

        engine.git.fetch = fetch
        recover(engine)

    kill_at(configured_factory, recovery, point)
    with factory(config) as engine:
        for _ in range(3):
            recovery(engine)
        assert engine.repos["demo"]["backup"]["status"] == "repair_paused"
        assert engine.repos["other"]["integrity"]["successful_repairs"] == 1
        assert engine.store.data["active_operation"]["alias"] == "other"
        burden.rename(tmp_path / "operator-preserved")
        for _ in range(3):
            recovery(engine)
        for alias in engine.repos:
            assert engine.git.healthy(engine.path(alias) / "repository.git")
            assert engine.repos[alias]["integrity"]["successful_repairs"] == 1
            assert engine.repos[alias]["integrity"]["repair_attempts"] == 0
            assert engine.repos[alias]["errors"]["total"] == 0
        assert (tmp_path / "operator-preserved").read_bytes() == b"x" * 65536


@pytest.mark.parametrize("phase", ["metadata", "journal"])
def test_paused_repair_handoff_io_error_keeps_journal(factory, monkeypatch, phase):
    import repoharbor.engine as engine_module

    mirror, refs = corrupt(factory)
    with factory() as engine:
        operation = engine.store.begin("demo", "repair")
        engine.store.update_operation(state="interrupted")
        engine.budget.config = replace(engine.budget.config, overrides={"demo": 0})
        original_begin = engine.store.begin
        begin = Mock(wraps=original_begin)
        monkeypatch.setattr(engine.store, "begin", begin)
        with monkeypatch.context() as local:
            target, name = (
                (engine_module, "atomic_json")
                if phase == "metadata"
                else (engine.store, "update_operation")
            )
            local.setattr(
                target, name, Mock(side_effect=OSError(errno.EIO, "synthetic"))
            )
            with pytest.raises(OSError):
                recover(engine)
        begin.assert_not_called()
        journal = read_json(factory.root / "data.json")["active_operation"]
        assert journal["id"] == operation and journal["state"] == "interrupted"
    with factory() as engine:
        recover(engine)
        assert engine.git.healthy(mirror)
        assert engine.git.refs(mirror) == refs
        assert engine.repos["demo"]["integrity"]["successful_repairs"] == 1
        assert engine.repos["demo"]["errors"]["total"] == 0
