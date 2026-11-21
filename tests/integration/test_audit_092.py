"""Regression fixtures for backup verification, publication and security settings."""

import errno
import os
import subprocess
import sys
import time
from dataclasses import replace
from threading import Event
from unittest.mock import Mock

import pytest
from conftest import git, raw_repo

from repoharbor import askpass, cli, integrity
from repoharbor.config import Discovery, Source, Storage, load
from repoharbor.credentials import read_token
from repoharbor.errors import GitError, HarborError, Result
from repoharbor.github import DiscoveryError, GitHub, parse_repository
from repoharbor.persistence import Store, UncertainCommit, move_new_directory, read_json
from repoharbor.status import repo_view
from repoharbor.storage import Budget


def config(yaml):
    return load(
        {
            "REPOHARBOR_CONFIG": f"schema_version: 1\n{yaml}",
            "REPOHARBOR_TOKEN_SOURCE": "file",
        }
    )


def test_missing_tree_never_reports_backup_success(factory):
    cfg = replace(factory.config, automatic_repair=False)
    with factory(cfg) as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        tree = git("rev-parse", "HEAD^{tree}", cwd=mirror)
        (mirror / "objects" / tree[:2] / tree[2:]).unlink()
        assert (
            integrity.run(engine, manual=True, repository="demo")
            == Result.INTEGRITY_ISSUES
        )
        assert engine.repos["demo"]["integrity"]["status"] == "corrupt"
    with factory(cfg) as engine:
        assert engine.run() == Result.BACKUP_FAILED
        assert (
            repo_view(cfg, "demo", engine.repos["demo"], True)["integrity_status"]
            == "corrupt"
        )


def test_unchanged_refs_require_object_verification(factory):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        tree = git("rev-parse", "HEAD^{tree}", cwd=mirror)
        object_file = mirror / "objects" / tree[:2] / tree[2:]
        object_file.unlink()
        object_file.write_bytes(b"invalid compressed object")
        assert engine.run() == Result.BACKUP_FAILED
        assert (
            engine.repos["demo"]["integrity"]["latest_verification"]["result"]
            == "corrupt"
        )


def test_discovery_scope_loss_and_intentional_exclusion(github_env):
    env = github_env
    env.results["first"] = [raw_repo(), raw_repo(rid=18, name="other")]
    with env.create(env.config, git_instance=env.git()) as engine:
        assert engine.run() == Result.SUCCESS
        old = engine.store.data["scheduler"]["last_successful_run_at"]
        env.results["first"] = [raw_repo()]
        assert engine.run() == Result.DISCOVERY_FAILED
        assert engine.store.data["scheduler"]["last_successful_run_at"] == old
        assert engine.store.data["active_run"]["coverage"]["unavailable"] == [
            "github:github.com:18"
        ]
        assert (engine.path("github:github.com:18") / "repository.git").exists()


def test_legacy_empty_catalog_entry_keeps_other_mirrors_available(factory):
    cfg = replace(
        factory.config,
        sources=(
            *factory.config.sources,
            Source("orphan", "https://example.org/orphan.git"),
        ),
    )
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        engine.path("orphan").mkdir()
    with factory(cfg) as engine:
        assert engine.run() == Result.NOT_SELECTED
        assert engine.path("orphan").exists()
        assert engine.repos["demo"]["backup"]["status"] == "up_to_date"


def test_inconclusive_json_commit_seals_writer(tmp_path, monkeypatch):
    with Store(tmp_path) as store:
        fsync = os.fsync

        def fail_directory(fd):
            if os.path.isdir(f"/proc/self/fd/{fd}"):
                raise OSError(errno.EIO, "fixture")
            fsync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_directory)
            with pytest.raises(UncertainCommit), store.transaction() as data:
                data["scheduler"]["skipped_overlaps"] = 7
        assert read_json(tmp_path / "data.json")["scheduler"]["skipped_overlaps"] == 7
        assert store.data["scheduler"]["skipped_overlaps"] == 7
        with pytest.raises(HarborError, match="restart required"):
            store.save()


def test_publication_preserves_existing_directory(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    with pytest.raises(FileExistsError):
        move_new_directory(source, target)
    assert source.is_dir() and target.is_dir()


def test_managed_mirror_rejects_external_config(factory, monkeypatch):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        git("config", "core.commentChar", ";", cwd=mirror)
        transfer = Mock(
            side_effect=AssertionError("repository policy evaluated too late")
        )
        monkeypatch.setattr(engine.git, "remote_refs", transfer)
        assert engine.run() == Result.BACKUP_FAILED
        transfer.assert_not_called()


def test_private_token_file_and_prompt_scope(tmp_path, monkeypatch, capsys):
    path = tmp_path / "token"
    path.write_bytes(b"synthetic-value\n")
    path.chmod(0o644)
    with pytest.raises(HarborError, match="private"):
        read_token(path)
    path.chmod(0o600)
    assert read_token(path) == "synthetic-value"
    path.write_bytes(b"x" * 8193)
    with pytest.raises(HarborError, match="size"):
        read_token(path)
    monkeypatch.setenv("REPOHARBOR_ASKPASS_AUTHORITY", "git.example.org:8443")
    monkeypatch.setattr(sys, "argv", ["askpass", "Unrecognized prompt"])
    with pytest.raises(SystemExit, match="1"):
        askpass.main()
    assert capsys.readouterr().out == ""


def test_yaml_duplicate_merge_and_calendar():
    with pytest.raises(HarborError, match="Duplicate"):
        config("storage: {min_free_gib: 1, min_free_gib: 0}")
    with pytest.raises(HarborError, match="Duplicate"):
        config("schedule: '* * * * *'\nschedule: '0 3 * * *'")
    with pytest.raises(HarborError, match="possible"):
        config("schedule: '0 3 31 2 *'")
    assert config("schedule: '0 3 29 2 *'").schedule == "0 3 29 2 *"
    assert (
        config(
            "storage: {<<: {min_free_gib: 1}, min_free_gib: 0}"
        ).storage.min_free_bytes
        == 0
    )


def test_multi_authority_credential_rejected_and_internal_accepted():
    source = (
        "credentials: [foo]\nmanual_git_urls:\n"
        "  - alias: first\n    url: https://one.example.org/a.git\n"
        "    credential: foo\n"
        "  - alias: second\n    credential: foo\n"
    )
    with pytest.raises(HarborError, match="another host"):
        config(source + "    url: https://two.example.org/b.git")
    assert (
        len(config(source + "    url: https://one.example.org:443/b.git").sources) == 2
    )
    assert (
        parse_repository(raw_repo(visibility="internal", private=True)).visibility
        == "internal"
    )


def test_provider_limits_and_cancellation(monkeypatch):
    from repoharbor.credentials import Credentials

    stop, entered, release = Event(), Event(), Event()
    client = GitHub(
        Credentials({}), stop=stop, request_delay_ms=Discovery(0).request_delay_ms
    )
    monkeypatch.setattr(client, "_request", Mock(side_effect=RecursionError))
    with pytest.raises(DiscoveryError, match="INVALID_RESPONSE"):
        client.request("/user", "unused")

    def stalled(*_):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(client, "_request", stalled)
    client.deadline = time.monotonic() + 0.2
    try:
        with pytest.raises(DiscoveryError, match="DISCOVERY_TIMEOUT"):
            client.request("/user", "unused")
        assert entered.is_set()
    finally:
        release.set()


def test_git_stream_limit_and_budget_scans(factory, monkeypatch, tmp_path):
    with factory() as engine:
        monkeypatch.setattr("repoharbor.git_backup.GIT_OUTPUT_MAX", 1024)
        with (
            subprocess.Popen(
                [sys.executable, "-c", "print('a' * 2048)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as proc,
            pytest.raises(GitError, match="GIT_OUTPUT_LIMIT"),
        ):
            from time import monotonic

            engine.git.command_output(proc, monotonic(), lambda: False, False, True)
    repo = tmp_path / "repo"
    repo.mkdir()
    from repoharbor import storage

    walk = Mock(wraps=storage.usage)
    monkeypatch.setattr(storage, "usage", walk)
    budget = Budget(tmp_path, Storage(min_free_bytes=0))
    with budget.batch():
        before = budget.measure("repo", repo)
        assert not before.global_estimate
        (repo / "new").write_bytes(b"123")
        measured = budget.measure("repo", repo)
        assert (
            measured.global_estimate
            and measured.global_bytes == before.global_bytes + 3
        )
    assert sum(call.args == (tmp_path,) for call in walk.call_args_list) == 1


def test_storage_expected_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOHARBOR_STORAGE_ID", "fixture-id")
    (tmp_path / ".repoharbor-storage-id").write_text("other-id\n")
    with pytest.raises(HarborError, match="does not match"):
        cli.verify_mount(tmp_path)
    (tmp_path / ".repoharbor-storage-id").write_text("fixture-id\n")
    cli.verify_mount(tmp_path)


def test_historic_manual_path_does_not_block_independent_discovery(github_env):
    env = github_env
    manual = Source("old", "https://github.com/old/project.git")
    with env.create(replace(env.config, sources=(manual,)), git_instance=env.git()):
        pass
    env.client.request.side_effect = DiscoveryError("GITHUB_NOT_ACCESSIBLE")
    with env.create(env.config, git_instance=env.git()) as engine:
        assert engine.run() == Result.SUCCESS
        assert engine.repos["old"]["selection"]["status"] == "excluded"
        assert (engine.path("old") / "metadaten.json").is_file()


def test_repair_inspection_failure_does_not_consume_attempt(factory, monkeypatch):
    from repoharbor.recovery import repair

    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        monkeypatch.setattr(engine.git, "fetch", Mock())
        monkeypatch.setattr(
            "repoharbor.recovery.inspect",
            Mock(return_value=("uncheckable", "FILESYSTEM_ERROR")),
        )
        repair(engine, "demo")
        meta = engine.repos["demo"]
        assert meta["integrity"]["repair_attempts"] == 0
        assert meta["backup"]["status"] == "repair_paused"
        assert meta["integrity"]["latest_verification"]["result"] == "uncheckable"


def test_empty_mirror_verification_distinguishes_bundle(factory, remote, tmp_path):
    work, bare = remote
    assert work.is_dir()
    empty = tmp_path / "empty.git"
    git("init", "--bare", "-b", "main", empty)
    with factory() as engine:
        engine.git.bare = empty
        assert engine.run() == Result.SUCCESS
        assert (
            engine.repos["demo"]["integrity"]["restore_compatibility"] == "clone_only"
        )
        assert engine.run() == Result.SUCCESS
        assert engine.path("demo").is_dir() and bare.is_dir()


@pytest.mark.parametrize("listing", ["one field", "not-an-object-id\trefs/heads/main"])
def test_git_ref_parser_reports_invalid_transport_output(factory, monkeypatch, listing):
    with factory() as engine:
        monkeypatch.setattr(engine.git, "run", Mock(return_value=listing))
        with pytest.raises(GitError, match="GIT_INVALID_REFS"):
            engine.git.remote_refs(factory.config.sources[0].url, None)


@pytest.mark.parametrize(
    "reason", ["missing-config", "alternate-store", "unsupported-format"]
)
def test_git_fsck_and_restore_capabilities(factory, reason):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        if reason == "missing-config":
            (mirror / "config").unlink()
            assert engine.git.restore_compatibility(mirror) == "unconfirmed"
        elif reason == "alternate-store":
            (mirror / "objects/info/alternates").touch()
            with pytest.raises(GitError, match="MIRROR_UNSUPPORTED"):
                engine.git.require_managed_config(mirror)
            assert engine.git.restore_compatibility(mirror) == "unconfirmed"
        else:
            git("config", "extensions.refStorage", "reftable", cwd=mirror)
            assert engine.git.restore_compatibility(mirror) == "unsupported"


@pytest.mark.parametrize("operation", ["before", "after"])
def test_store_guard_tracks_publication_phase(tmp_path, monkeypatch, operation):
    from repoharbor import persistence

    with Store(tmp_path) as store:
        old_replace = os.replace

        def fail_rename(*a, **kw):
            if operation == "after":
                old_replace(*a, **kw)
                # The supported Linux rename either returns success or does not
                # publish. Simulate the next durable boundary on the descriptor.
                return
            raise OSError(errno.ENOSPC, "fixture")

        if operation == "before":
            with monkeypatch.context() as patch:
                patch.setattr(persistence.os, "replace", fail_rename)
                with pytest.raises(OSError), store.transaction() as data:
                    data["scheduler"]["skipped_overlaps"] = 2
            assert store.data["scheduler"]["skipped_overlaps"] == 0
            store.save()
        else:
            store.begin("demo", "initial_clone")
            with pytest.raises(HarborError, match="unfinished"):
                store.begin("another", "mirror_update")
            assert (
                read_json(tmp_path / "data.json")["active_operation"]["alias"] == "demo"
            )


def test_v091_journal_and_mirror_are_reused(factory, remote):
    import shutil
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "fixtures/v091"
    root = factory.root
    shutil.copyfile(fixture / "journal.json", root / "data.json")
    folder = root / "manual/demo"
    folder.mkdir(parents=True)
    shutil.copyfile(fixture / "repository.json", folder / "metadaten.json")
    mirror = folder / "repository.git"
    git("clone", "--mirror", remote[1], mirror)
    before = mirror.stat().st_ino
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        assert mirror.stat().st_ino == before
        assert (
            engine.repos["demo"]["integrity"]["restore_compatibility"]
            == "clone_and_bundle"
        )


@pytest.mark.parametrize(
    "change", [{"planned": -1}, {"attempted": 2}, {"unavailable": [7]}, {"missing": 2}]
)
def test_invalid_run_coverage_is_rejected(factory, change):
    from repoharbor.persistence import validate_state

    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        engine.store.data["active_run"]["coverage"].update(change)
        with pytest.raises(HarborError, match="journal"):
            validate_state(engine.store.data)


@pytest.mark.parametrize("history", [{"invalid": {}}, {"integrity": {"result": False}}])
def test_invalid_check_history_is_rejected(factory, history):
    from repoharbor.persistence import validate_state

    with factory() as engine:
        engine.store.data["integrity_history"] = history
        with pytest.raises(HarborError, match="journal"):
            validate_state(engine.store.data)


def test_manual_identity_claim_survives_rename_and_removal(github_env):
    env = github_env
    source = Source("historical", "https://github.com/fictional/project.git")
    with env.create(
        replace(env.config, sources=(source,)), git_instance=env.git()
    ) as engine:
        engine.discover()
        assert engine.repos["historical"]["repository"]["identity_claims"] == [env.key]
    env.results["first"] = [
        raw_repo(name="renamed"),
        raw_repo(rid=23, name="independent"),
    ]
    env.results["second"] = env.results["first"]
    env.client.request.reset_mock()
    with env.create(env.config, git_instance=env.git()) as engine:
        assert engine.run() == Result.SUCCESS
        assert (
            engine.repos[env.key]["selection"]["reason"] == "manual_identity_conflict"
        )
        assert len(engine.sources) == 1
        env.client.request.assert_not_called()


def test_finish_soft_limit_blocks_future_transfers_until_bytes_are_released(
    factory, remote, monkeypatch
):
    cfg = replace(
        factory.config, storage=Storage(repo_bytes=1024 * 1024, min_free_bytes=0)
    )
    with factory(cfg) as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        git("update-ref", "refs/heads/expanded", "refs/heads/main", cwd=remote[1])
        fetch = engine.git.fetch
        burden = engine.path("demo") / "retained-bytes"

        def grow(mirror, url, credential, monitor):
            with burden.open("wb") as data:
                data.truncate(2 * 1024 * 1024)
            assert monitor() is False
            return fetch(mirror, url, credential, monitor)

        with monkeypatch.context() as patch:
            patch.setattr(engine.git, "fetch", grow)
            assert engine.run() == Result.SUCCESS
        assert "refs/heads/expanded" in engine.git.refs(mirror)
        assert engine.repos["demo"]["backup"]["successful_updates"] == 2
        assert engine.run() == Result.BACKUP_FAILED
        assert engine.repos["demo"]["backup"]["status"] == "repo_limit_reached"
        assert burden.exists()
        burden.unlink()
        assert engine.run() == Result.SUCCESS


def test_exact_global_admission_counts_growth_outside_current_repository(
    factory, remote, monkeypatch
):
    from repoharbor.storage import usage

    cfg = replace(
        factory.config, storage=Storage(global_bytes=32 * 1024**2, min_free_bytes=0)
    )
    with factory(cfg) as engine:
        assert engine.run() == Result.SUCCESS
        retained = cfg.root / "operator-archive"
        retained.mkdir()
        burden = retained / "quarantined-content"
        refs = engine.git.remote_refs
        git("update-ref", "refs/heads/new", "refs/heads/main", cwd=remote[1])

        def grow(*args):
            with burden.open("wb") as data:
                data.truncate(32 * 1024**2)
            return refs(*args)

        assert usage(cfg.root) < 16 * 1024**2
        with monkeypatch.context() as patch:
            patch.setattr(engine.git, "remote_refs", grow)
            fetch = Mock(wraps=engine.git.fetch)
            patch.setattr(engine.git, "fetch", fetch)
            assert engine.run() == Result.BACKUP_FAILED
            fetch.assert_not_called()
        assert engine.repos["demo"]["backup"]["status"] == "global_limit_paused"
        assert burden.exists()
        burden.unlink()
        assert engine.run() == Result.SUCCESS


def test_mixed_run_preserves_corrupt_excluded_and_paused_mirrors(factory, remote):
    from conftest import LocalGit

    sources = (
        *factory.config.sources,
        *(
            Source(name, f"https://example.org/{name}.git")
            for name in ("broken", "excluded", "paused")
        ),
    )

    class LocalMirrors(LocalGit):
        def run(self, args, *a, **kwargs):
            return super().run(
                [
                    sources[0].url if arg in {s.url for s in sources} else arg
                    for arg in args
                ],
                *a,
                **kwargs,
            )

    cfg = replace(factory.config, sources=sources, automatic_repair=False)
    with factory(cfg, git_instance=LocalMirrors(remote[1])) as engine:
        assert engine.run() == Result.SUCCESS
        broken = engine.path("broken") / "repository.git"
        oid = git("rev-parse", "HEAD:hello.txt", cwd=broken)
        (broken / "objects" / oid[:2] / oid[2:]).unlink()
    git("update-ref", "refs/heads/new", "refs/heads/main", cwd=remote[1])
    limited = replace(
        cfg,
        sources=tuple(s for s in sources if s.alias != "excluded"),
        storage=replace(cfg.storage, overrides={"paused": 0}),
    )
    with factory(limited, git_instance=LocalMirrors(remote[1])) as engine:
        assert engine.run() == Result.BACKUP_FAILED
        assert engine.repos["demo"]["backup"]["successful_updates"] == 2
        assert engine.repos["broken"]["integrity"]["status"] == "corrupt"
        assert engine.repos["paused"]["backup"]["status"] == "repo_limit_reached"
        assert engine.repos["excluded"]["selection"]["status"] == "excluded"
        for alias in ("broken", "paused", "excluded"):
            assert not (engine.path(alias) / "repository.git/refs/heads/new").exists()

    global_limit = replace(limited, storage=replace(cfg.storage, global_bytes=0))
    git("update-ref", "refs/heads/later", "refs/heads/main", cwd=remote[1])
    with factory(global_limit, git_instance=LocalMirrors(remote[1])) as engine:
        assert engine.run() == Result.BACKUP_FAILED
        for alias in ("demo", "paused"):
            assert engine.repos[alias]["backup"]["status"] == "global_limit_paused"
            assert not (engine.path(alias) / "repository.git/refs/heads/later").exists()
        assert not any("fetch" in args for args in engine.git.calls)
