import json
import threading
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from conftest import git, raw_project, raw_repo

from repoharbor import integrity, status
from repoharbor.control import Control, request
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError, Result
from repoharbor.git_backup import Git
from repoharbor.github import DiscoveryError, parse_repository
from repoharbor.gitlab import parse_project
from repoharbor.persistence import record_error, utcnow
from repoharbor.providers import Observation
from repoharbor.recovery import recover
from repoharbor.registry import plan
from repoharbor.scheduler import Cron, Scheduler


def snapshot(folder):
    return {
        p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    }


def blob_path(mirror):
    oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
    return mirror / "objects" / oid[:2] / oid[2:]


def test_manual_checks_all_catalog_mirrors_without_remote_or_repair(factory, remote):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        integrity.run(engine)
        original = engine.repos["demo"]
        for alias in ("excluded", "removed", "partial", "uncheckable"):
            meta = deepcopy(original)
            meta["repository"].update(id=alias, name=alias, aliases=[alias])
            meta["selection"]["status"] = "excluded"
            meta["source"]["status"] = "unavailable"
            path = engine.path(alias)
            path.mkdir()
            engine.repos[alias] = meta
            git("clone", "--mirror", remote[1], path / "repository.git")
            engine.save_meta(alias)
        mirror = engine.path("excluded") / "repository.git"
        (mirror / "refs/heads/broken").write_text("invalid ref\n")
        (engine.path("partial") / "repository.git.partial").mkdir()
        git(
            "config",
            "fsck.missingEmail",
            "ignore",
            cwd=engine.path("uncheckable") / "repository.git",
        )
        previous = deepcopy(engine.store.data)
        before = {
            alias: snapshot(engine.path(alias) / "repository.git")
            for alias in engine.repos
        }
        config = replace(engine.config, sources=())
    with factory(config) as engine:
        assert not engine.config.integrity_check
        assert (
            integrity.run(engine, manual=True, repository="unknown")
            == Result.UNKNOWN_REPOSITORY
        )
        assert (
            integrity.run(engine, manual=True, repository="partial")
            == Result.INTEGRITY_SKIPPED
        )
        assert (
            integrity.run(engine, manual=True, repository="excluded")
            == Result.INTEGRITY_ISSUES
        )
        assert (
            integrity.run(engine, manual=True, repository="excluded")
            == Result.INTEGRITY_ISSUES
        )
        assert (
            integrity.run(engine, manual=True, repository="removed") == Result.SUCCESS
        )
        assert integrity.run(engine, manual=True) == Result.INTEGRITY_ISSUES
        state = engine.store.data["manual_integrity"]
        assert state["target"] is None
        assert [
            state[key]
            for key in ("checked", "healthy", "corrupt", "skipped", "uncheckable")
        ] == [3, 2, 1, 1, 1]
        assert all(engine.store.data[key] == value for key, value in previous.items())
        assert all(
            not {"fetch", "clone", "ls-remote"}.intersection(call)
            for call in engine.git.calls
        )
        assert before == {
            alias: snapshot(engine.path(alias) / "repository.git")
            for alias in engine.repos
        }
    with factory(config) as engine:
        assert engine.store.data["manual_integrity"] == state
        saved = engine.repos["excluded"]["integrity"]
        assert saved["manual"]["last_result"] == "corrupt"
        assert saved["manual"]["error_code"] == "GIT_CORRUPTION"
        assert saved["periodic"] == original["integrity"]["periodic"]
        assert not saved["periodic"]["repair_pending"]


def test_manual_check_stop_and_restart_preserve_previous_completed_finding(factory):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        assert integrity.run(engine, manual=True) == Result.SUCCESS
        previous = engine.repos["demo"]["integrity"]["manual"]["last_completed_at"]

        def stop(name):
            if name == "integrity_started":
                engine.git.stop.set()

        engine.checkpoint = stop
        assert (
            integrity.run(engine, manual=True, repository="demo")
            == Result.INTEGRITY_INTERRUPTED
        )
    with factory() as engine:
        integrity.reconcile(engine.store)
        state = engine.store.data["manual_integrity"]
        assert state["state"] == "interrupted" and state["checked"] == 0
        result = engine.repos["demo"]["integrity"]["manual"]
        assert result["last_completed_at"] == previous
        assert result["last_started_at"] > previous
        assert result["last_result"] == "healthy"


def test_manual_check_empty_catalog(factory):
    with factory(replace(factory.config, sources=())) as engine:
        assert integrity.run(engine, manual=True) == Result.INTEGRITY_SKIPPED
        assert engine.store.data["manual_integrity"]["last_result"] == "no_candidates"


def test_manual_integrity_owns_lock_and_skips_overlapping_schedule(
    factory, monkeypatch
):
    initial = datetime(2026, 1, 1, tzinfo=UTC)
    cron = Cron("* * * * *")
    backup = Scheduler(cron, "UTC", initial)
    config = replace(factory.config, manual_run=True)
    with factory(config) as engine:
        assert engine.run() == Result.SUCCESS
        entered, release = threading.Event(), threading.Event()
        inspect = integrity.inspect

        def pause(git, mirror):
            entered.set()
            assert release.wait(5)
            return inspect(git, mirror)

        monkeypatch.setattr(integrity, "inspect", pause)
        scheduled = Scheduler(
            cron,
            "UTC",
            initial,
            lambda count: integrity.skip_overlap(engine.store, count),
            busy=backup.busy,
        )
        control = Control(engine, backup, engine.git.stop)
        with control.start(), ThreadPoolExecutor() as pool:
            run = pool.submit(request, config.root, b"check repo demo")
            try:
                assert entered.wait(3)
                state = engine.store.data["manual_integrity"]
                assert (
                    state["state"] == "running"
                    and state["current_repository"] == "demo"
                )
                assert request(config.root, b"all") == Result.BUSY
                assert request(config.root, b"check all") == Result.BUSY
                periodic = Mock()
                assert not scheduled.tick(initial + timedelta(minutes=1), periodic)
                periodic.assert_not_called()
                assert engine.store.data["integrity"]["skipped_overlaps"] == 1
            finally:
                release.set()
            assert run.result(timeout=5) == Result.SUCCESS


@pytest.mark.parametrize(
    "damage,expected",
    [
        ("none", ("healthy", None)),
        ("empty", ("healthy", None)),
        ("missing-head-target", ("healthy", None)),
        ("dangling", ("healthy", None)),
        ("missing-object", ("corrupt", "GIT_CORRUPTION")),
        ("bad-object", ("corrupt", "GIT_CORRUPTION")),
        ("orphan-pack", ("corrupt", "GIT_CORRUPTION")),
        ("orphan-index", ("corrupt", "GIT_CORRUPTION")),
        ("bad-pack", ("corrupt", "GIT_CORRUPTION")),
        ("bad-ref", ("corrupt", "GIT_CORRUPTION")),
        ("non-bare", ("corrupt", "INVALID_MIRROR")),
        ("file", ("corrupt", "INVALID_MIRROR")),
        ("missing", ("uncheckable", "MIRROR_MISSING")),
        ("alternate", ("uncheckable", "MIRROR_UNSUPPORTED")),
        ("fsck-override", ("uncheckable", "MIRROR_UNSUPPORTED")),
        ("include", ("uncheckable", "MIRROR_UNSUPPORTED")),
    ],
)
def test_real_git_findings_preserve_every_mirror_file(
    tmp_path, remote, damage, expected
):
    work, _ = remote
    mirror = tmp_path / "mirror.git"
    if damage == "missing":
        pass
    elif damage == "file":
        mirror.write_text("preserved")
    elif damage == "non-bare":
        git("init", mirror)
        mirror = mirror / ".git"
        git("config", "core.bare", "false", cwd=mirror)
    elif damage == "empty":
        git("init", "--bare", mirror)
    else:
        git("branch", "other", cwd=work)
        git("tag", "test-tag", cwd=work)
        git("clone", "--mirror", "--no-hardlinks", work, mirror)
        if damage == "missing-head-target":
            git("symbolic-ref", "HEAD", "refs/heads/deleted", cwd=mirror)
        elif damage == "dangling":
            blob = tmp_path / "unreachable"
            blob.write_text("old object")
            git("hash-object", "-w", blob, cwd=mirror)
        elif damage == "missing-object":
            blob_path(mirror).unlink()
        elif damage == "bad-object":
            blob_path(mirror).write_bytes(b"bad Git object")
        elif damage == "bad-pack":
            git("repack", "-ad", cwd=mirror)
            pack = next((mirror / "objects/pack").glob("*.pack"))
            pack.chmod(0o644)
            pack.write_bytes(pack.read_bytes()[:-10])
        elif damage.startswith("orphan-"):
            git("repack", "-ad", cwd=mirror)
            suffix = ".idx" if damage == "orphan-pack" else ".pack"
            next((mirror / "objects/pack").glob(f"*{suffix}")).unlink()
        elif damage == "bad-ref":
            (mirror / "refs/heads/broken").write_text("bad reference\n")
        elif damage == "alternate":
            (mirror / "objects/info/alternates").write_text("/outside\n")
        elif damage == "fsck-override":
            git("config", "fsck.missingEmail", "ignore", cwd=mirror)
        elif damage == "include":
            git("config", "include.path", "/outside", cwd=mirror)
    before = snapshot(mirror)
    checker = Git(Credentials({}), threading.Event())
    assert integrity.inspect(checker, mirror) == expected
    assert snapshot(mirror) == before


@pytest.mark.parametrize("include_excluded", [True, False])
def test_catalog_is_local_complete_deterministic_and_credential_free(
    factory, remote, monkeypatch, include_excluded
):
    config = replace(
        factory.config,
        integrity=replace(
            factory.config.integrity,
            include_excluded=include_excluded,
        ),
    )
    found = [
        parse_repository(raw_repo()),
        *(
            parse_project(raw_project(host=host), host)
            for host in ("gitlab.com", "gitlab.example.org")
        ),
    ]
    with factory(config) as engine:
        assert engine.run() == Result.SUCCESS
        for repo in found:
            entries, _ = plan(
                config,
                engine.repos,
                [Observation(repo, "unused", True)],
                set(),
                host=repo.host,
                provider_type=repo.provider,
            )
            meta = entries[repo.key]
            meta["backup"].update(created_at=utcnow(), successful_updates=1)
            meta["source"]["status"] = "unavailable"
            engine.repos[repo.key] = meta
            folder = engine.path(repo.key)
            folder.mkdir(parents=True)
            git("clone", "--mirror", remote[1], folder / "repository.git")
            engine.save_meta(repo.key)

    no_credentials = Git(Credentials({}), threading.Event())
    with factory(config, git_instance=no_credentials) as engine:
        blocked = Mock(side_effect=AssertionError("periodic network or recovery"))
        monkeypatch.setattr(engine, "discover", blocked)
        monkeypatch.setattr(no_credentials, "fetch", blocked)
        monkeypatch.setattr(no_credentials, "remote_refs", blocked)
        checked = []
        inspect = integrity.inspect

        def observe(checker, mirror):
            checked.append(mirror)
            return inspect(checker, mirror)

        monkeypatch.setattr(integrity, "inspect", observe)
        before = deepcopy(engine.store.data)
        sources = engine.sources
        candidates = sorted(engine.repos) if include_excluded else ["demo"]
        integrity.run(engine)
        assert checked == [
            engine.path(alias) / "repository.git" for alias in candidates
        ]
        assert engine.sources == sources
        assert engine.store.data["integrity"]["healthy"] == len(candidates)
        assert engine.store.data["integrity"]["skipped"] == len(engine.repos) - len(
            candidates
        )
        assert all(engine.store.data[key] == value for key, value in before.items())
        blocked.assert_not_called()


@pytest.mark.parametrize("mode", ["full", "target", "excluded", "repair-disabled"])
def test_corruption_only_repaired_during_later_eligible_backup(factory, mode):
    config = factory.config
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        refs = engine.git.refs(mirror)
        blob_path(mirror).unlink()
        operation = deepcopy(engine.store.data["active_operation"])
        calls = len(engine.git.calls)
        integrity.run(engine)
        assert all(
            "fetch" not in call and "ls-remote" not in call
            for call in engine.git.calls[calls:]
        )
        assert engine.store.data["active_operation"] == operation
        assert engine.repos["demo"]["backup"]["successful_updates"] == 1
        assert (
            engine.repos["demo"]["integrity"]["periodic"]["error_code"]
            == "GIT_CORRUPTION"
        )
    if mode == "excluded":
        config = replace(config, sources=())
    if mode == "repair-disabled":
        config = replace(config, automatic_repair=False)
    with factory(config) as engine:
        recover(engine)
        assert engine.git.calls == []
        result = engine.run(
            manual=mode == "target", repository="demo" if mode == "target" else None
        )
        state = engine.repos["demo"]["integrity"]
        if mode in ("full", "target"):
            assert result == Result.SUCCESS
            assert state["successful_repairs"] == 1 and state["repair_attempts"] == 0
            assert state["last_check_kind"] == "repair"
            assert not state["periodic"]["repair_pending"]
            assert engine.git.refs(mirror) == refs
            integrity.run(engine)
            assert state["periodic"]["last_result"] == "healthy"
        else:
            assert result != Result.SUCCESS
            assert state["status"] == "corrupt"
            assert state["repair_attempts"] == 0
            assert not any("fetch" in args for args in engine.git.calls)


@pytest.mark.parametrize("fixture", ["github_env", "gitlab_env"])
def test_provider_repair_requires_current_successful_discovery(request, fixture):
    env = request.getfixturevalue(fixture)
    with env.create(env.config, git_instance=env.git()) as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path(env.key) / "repository.git"
        blob_path(mirror).unlink()
        integrity.run(engine)
        original = env.client.discover.side_effect
        env.client.discover.side_effect = DiscoveryError("GITHUB_TEST_FAILURE")
        assert engine.run() == Result.DISCOVERY_FAILED
        assert not any("fetch" in args for args in engine.git.calls)
        assert engine.repos[env.key]["integrity"]["periodic"]["repair_pending"]
        env.client.discover.side_effect = original
        assert engine.run() == Result.SUCCESS
        assert engine.repos[env.key]["integrity"]["successful_repairs"] == 1


def test_absent_unfinished_and_previously_damaged_repositories(factory):
    from repoharbor.config import Source

    config = replace(
        factory.config,
        sources=factory.config.sources
        + tuple(
            Source(alias, f"https://example.org/{alias}.git")
            for alias in ("never", "partial", "quarantine", "gone")
        ),
    )
    with factory(config) as engine:
        assert engine.run(manual=True, repository="demo") == Result.SUCCESS
        metadata = engine.repos
        gone = metadata["gone"]["backup"]
        gone["created_at"] = utcnow()
        partial = engine.path("partial") / "repository.git.partial"
        partial.mkdir()
        preserved = engine.path("quarantine") / "quarantine/initial-clone"
        preserved.mkdir(parents=True)
        metadata["demo"]["integrity"]["status"] = "repair_failed"
        metadata["demo"]["integrity"]["repair_attempts"] = 3
        before = {key: deepcopy(meta["backup"]) for key, meta in metadata.items()}
        operation = engine.store.begin("demo", "repair")
        engine.store.update_operation(state="interrupted")
        integrity.run(engine)
        assert engine.store.data["integrity"]["skipped"] == 3
        assert engine.store.data["integrity"]["uncheckable"] == 1
        assert engine.store.data["integrity"]["healthy"] == 1
        state = metadata["demo"]["integrity"]
        assert state["periodic"]["last_result"] == "healthy"
        assert state["repair_attempts"] == 3 and state["status"] == "repair_failed"
        assert (
            metadata["gone"]["integrity"]["periodic"]["error_code"] == "MIRROR_MISSING"
        )
        assert all(meta["backup"] == before[key] for key, meta in metadata.items())
        assert partial.is_dir() and preserved.is_dir()
        assert engine.store.data["active_operation"]["id"] == operation
        assert engine.store.data["active_operation"]["state"] == "interrupted"
        assert all(
            "periodic" not in metadata[key]["integrity"]
            for key in ("partial", "quarantine", "never")
        )


def test_empty_catalog_and_unsafe_mirror_path(factory):
    with factory() as engine:
        integrity.run(engine)
        assert engine.store.data["integrity"]["last_result"] == "no_candidates"
        mirror = engine.path("demo") / "repository.git"
        mirror.mkdir()
        (mirror / "unsafe").symlink_to("/outside")
        assert integrity.inspect(engine.git, mirror) == (
            "uncheckable",
            "FILESYSTEM_ERROR",
        )


def test_repair_cannot_declare_orphan_pack_healthy(factory):
    config = replace(factory.config, repair_attempts=1)
    with factory(config) as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        orphan = mirror / "objects/pack" / ("pack-" + "0" * 40 + ".pack")
        orphan.write_bytes(b"orphaned pack")
        integrity.run(engine)
        state = engine.repos["demo"]["integrity"]
        assert state["status"] == "corrupt"
        assert engine.run() == Result.BACKUP_FAILED
        assert state["status"] == "repair_failed"
        assert state["repair_attempts"] == 1
        assert engine.run() == Result.BACKUP_FAILED
        assert state["repair_attempts"] == 1
        assert orphan.read_bytes() == b"orphaned pack"


def test_filters_apply_with_only_local_provider_metadata(github_env, monkeypatch):
    from repoharbor.config import Provider
    from repoharbor.selection import Selection

    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        selection = engine.repos[env.key]["selection"].copy()
        assert selection["status"] == "included"
        config = replace(
            engine.config,
            integrity=replace(engine.config.integrity, include_excluded=False),
        )
        engine.config = config
        original = integrity.inspect
        probe = Mock(wraps=original)
        monkeypatch.setattr(integrity, "inspect", probe)
        integrity.run(engine)
        assert probe.call_count == 1
        engine.config = replace(
            config, selection=Selection(mode="all", excluded=(env.key,))
        )
        integrity.run(engine)
        assert engine.store.data["integrity"]["skipped"] == 1
        engine.config = replace(
            config,
            providers=(Provider("other", "second", type="gitlab", host="gitlab.com"),),
        )
        integrity.run(engine)
        assert engine.store.data["integrity"]["skipped"] == 1
        assert probe.call_count == 1
        assert engine.repos[env.key]["selection"] == selection


def test_uncertain_periodic_recovery_cannot_replace_an_unresolved_journal(
    factory,
    monkeypatch,
):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        blob_path(mirror).unlink()
        integrity.run(engine)
        uncertain = Mock(return_value=("uncheckable", "FILESYSTEM_ERROR"))
        monkeypatch.setattr("repoharbor.recovery.inspect", uncertain)
        assert engine.run() == Result.BACKUP_FAILED
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 0
        assert not any("fetch" in args for args in engine.git.calls)
        journal = engine.store.begin("demo", "repair")
        with pytest.raises(HarborError, match="Cannot verify interrupted"):
            recover(engine)
        assert engine.store.data["active_operation"]["id"] == journal
        assert engine.store.data["active_operation"]["state"] == "running"


@pytest.mark.parametrize("excluded", [False, True])
def test_offline_object_restoration_clears_periodic_damage(
    factory,
    monkeypatch,
    excluded,
):
    config = replace(factory.config, sources=()) if excluded else factory.config
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        blob = blob_path(mirror)
        contents = blob.read_bytes()
        blob.unlink()
        integrity.run(engine)
        state = engine.repos["demo"]["integrity"]
        assert state["status"] == "corrupt"
        assert state["periodic"]["repair_pending"]
        damage = state["periodic"]["damaged_at"]
    # Repair the real object while RepoHarbor has released the root lock.
    blob.write_bytes(contents)
    before_files = snapshot(mirror)
    with factory(config) as engine:
        before = deepcopy(engine.repos["demo"])
        scheduler = deepcopy(engine.store.data["scheduler"])
        with monkeypatch.context() as local:
            for method in ("fetch", "remote_refs", "clone"):
                local.setattr(
                    engine.git, method, Mock(side_effect=AssertionError(method))
                )
            local.setattr(
                engine, "discover", Mock(side_effect=AssertionError("discovery"))
            )
            integrity.run(engine)
        after = engine.repos["demo"]
        state = after["integrity"]
        assert state["status"] == "healthy"
        assert state["last_check_kind"] == "periodic"
        assert state["last_checked_at"] == state["periodic"]["last_completed_at"]
        assert state["periodic"]["last_result"] == "healthy"
        assert not state["periodic"]["repair_pending"]
        assert state["periodic"]["damaged_at"] == damage
        assert state["successful_repairs"] == 0
        for key in ("backup", "errors", "selection", "source"):
            assert after[key] == before[key]
        assert after["selection"]["status"] == ("excluded" if excluded else "included")
        assert engine.store.data["scheduler"] == scheduler
        assert snapshot(mirror) == before_files
        if not excluded:
            assert engine.run() == Result.SUCCESS
            assert after["backup"]["status"] == "up_to_date"


@pytest.mark.parametrize("previous", ["repair_failed", "repair_paused", "suspect"])
def test_verified_restore_retires_repair_state_only(
    factory,
    previous,
    capsys,
    monkeypatch,
):
    config = replace(factory.config, repair_attempts=1)
    with factory(config) as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        blob = blob_path(mirror)
        contents = blob.read_bytes()
        blob.unlink()
        integrity.run(engine)
        meta = engine.repos["demo"]
        if previous == "repair_failed":
            # Exhaust a real repair incident by making the source unreachable.
            with monkeypatch.context() as local:
                local.setattr(
                    engine.git, "fetch", Mock(side_effect=HarborError("GIT_FAILED"))
                )
                assert engine.run() == Result.BACKUP_FAILED
            assert meta["integrity"]["status"] == "repair_failed"
            assert meta["backup"]["status"] == "repair_failed"
        elif previous == "repair_paused":
            meta["backup"].update(
                status="repair_paused",
                repair_deferred="repo_limit_reached",
                storage_status="repo_limit_reached",
            )
        else:
            meta["integrity"]["status"] = "suspect"
            record_error(meta, "GIT_FAILED")
        engine.save_meta("demo")
        before = deepcopy(meta)
        scheduler = deepcopy(engine.store.data["scheduler"])
    blob.write_bytes(contents)
    with factory(config) as engine:
        integrity.run(engine)
        actual = engine.repos["demo"]
        state = actual["integrity"]
        assert state["status"] == "healthy"
        assert state["periodic"]["repair_pending"] is False
        assert state["repair_attempts"] == 0
        for key in (
            "last_repair_at",
            "last_repair_result",
            "completed_repair_operation",
            "successful_repairs",
        ):
            assert state.get(key) == before["integrity"].get(key)
        for key in ("total", "last", "recent"):
            assert actual["errors"][key] == before["errors"][key]
        assert actual["errors"]["consecutive"] == int(previous == "suspect")
        for key in (
            "last_checked_at",
            "last_updated_at",
            "last_attempt_at",
            "successful_updates",
        ):
            assert actual["backup"][key] == before["backup"][key]
        assert "repair_deferred" not in actual["backup"]
        assert (
            actual["backup"]["status"]
            == {
                "repair_failed": "pending",
                "repair_paused": "repo_limit_reached",
                "suspect": "updated",
            }[previous]
        )
        assert engine.store.data["scheduler"] == scheduler

        if previous == "repair_failed":
            for command, action in (
                ("status", None),
                ("repositories", "list"),
                ("repositories", "show"),
            ):
                args = Namespace(
                    command=command, action=action, repository="demo", json=False
                )
                assert status.print_status(config, args) == Result.SUCCESS
                output = capsys.readouterr().out
                assert "repair_failed" not in output
                assert "healthy" in output
                args.json = True
                assert status.print_status(config, args) == Result.SUCCESS
                view = json.loads(capsys.readouterr().out)
                if command == "status":
                    assert view["repositories"]["with_errors"] == 0
                    assert view["periodic_integrity"]["healthy"] == 1
                else:
                    if action == "list":
                        view = view["repositories"][0]
                    assert view["integrity_status"] == "healthy"
                    assert view["backup_status"] == "pending"
                    assert view["last_error_code"] == "REPAIR_FAILED"
            blob.unlink()
            integrity.run(engine)
            assert state["status"] == "corrupt"
            assert state["periodic"]["repair_pending"]
            assert engine.run() == Result.SUCCESS
            assert state["successful_repairs"] == 1
        elif previous == "repair_paused":
            engine.budget.config = replace(engine.config.storage, repo_bytes=0)
            before_calls = len(engine.git.calls)
            assert engine.run() == Result.BACKUP_FAILED
            assert actual["backup"]["status"] == "repo_limit_reached"
            assert engine.git.calls[before_calls:] == []


@pytest.mark.parametrize(
    "block, finding",
    [
        ("journal", "healthy"),
        ("backup-run", "healthy"),
        ("quarantine", None),
        ("manual-attention", "healthy"),
        ("repair-journal", "healthy"),
        ("repairing", "healthy"),
        ("interrupted", "healthy"),
        ("partial", "healthy"),
        ("partial-access", "healthy"),
        ("partial-operation", None),
        ("unknown-deferral", "healthy"),
        ("uncheckable", "uncheckable"),
        ("corrupt", "corrupt"),
    ],
)
def test_recheck_preserves_unresolved_state(factory, monkeypatch, block, finding):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        folder = engine.path("demo")
        mirror = folder / "repository.git"
        partial = folder / "repository.git.partial"
        blob = blob_path(mirror)
        contents = blob.read_bytes()
        blob.unlink()
        integrity.run(engine)
        meta = engine.repos["demo"]
        state = meta["integrity"]
        assert state["periodic"]["repair_pending"]
        state["repair_attempts"] = 2
        if block != "corrupt":
            blob.write_bytes(contents)
        if block in {"journal", "repair-journal"}:
            engine.store.begin(
                "demo", "repair" if block == "repair-journal" else "mirror_update"
            )
            if block == "repair-journal":
                engine.store.update_operation(state="interrupted")
        elif block == "backup-run":
            with engine.store.transaction() as data:
                data["active_run"].update(state="running")
        elif block == "quarantine":
            meta["backup"]["quarantine"] = {
                "state": "pending",
                "operation_id": "old-operation",
                "relative_path": "quarantine/initial-clone",
            }
        elif block == "manual-attention":
            meta["backup"]["manual_attention_required"] = "QUARANTINE_CONFLICT"
        elif block in {"repairing", "interrupted"}:
            meta["backup"]["status"] = block
        elif block == "partial":
            partial.mkdir()
            (partial / "precious").write_text("preserve")
        elif block == "partial-access":
            original = type(partial).lstat

            def lstat(path):
                if path == partial:
                    raise PermissionError("partial state unavailable")
                return original(path)

            monkeypatch.setattr(type(partial), "lstat", lstat)
        elif block == "partial-operation":
            meta["backup"]["partial_operation"] = "old-operation"
        elif block == "unknown-deferral":
            meta["backup"]["repair_deferred"] = "UNRESOLVED_CONFLICT"
        elif block == "uncheckable":
            (mirror / "objects/info/alternates").write_text("/unavailable\n")

        journal = deepcopy(engine.store.data["active_operation"])
        backup_run = deepcopy(engine.store.data["active_run"])
        before = deepcopy(meta["backup"])
        previous = deepcopy(state["periodic"])
        backup_errors = deepcopy(meta["errors"])
        integrity.run(engine)
        assert state["status"] == "corrupt"
        assert state["repair_attempts"] == 2
        assert state["periodic"]["repair_pending"]
        if finding is None:
            assert state["periodic"] == previous
        else:
            assert state["periodic"]["last_result"] == finding
        assert meta["backup"] == before
        assert meta["errors"] == backup_errors
        assert engine.store.data["active_operation"] == journal
        assert engine.store.data["active_run"] == backup_run
        if block == "partial":
            assert (partial / "precious").read_text() == "preserve"


def test_recheck_resolves_legacy_repair_block_after_reconciliation(factory):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
        meta = engine.repos["demo"]
        meta["backup"].update(
            status="repair_failed", repair_deferred="DISCOVERY_REQUIRED"
        )
        meta["integrity"].update(status="repair_failed", repair_attempts=3)
        record_error(meta, "GIT_FAILED")
        record_error(meta, "REPAIR_FAILED")
        engine.store.begin("demo", "repair")
        engine.store.update_operation(state="reconciled")
        journal = deepcopy(engine.store.data["active_operation"])
        assert "periodic" not in meta["integrity"]
        integrity.run(engine)
        assert meta["integrity"]["status"] == "healthy"
        assert meta["backup"]["status"] == "pending"
        assert "repair_deferred" not in meta["backup"]
        assert meta["integrity"]["repair_attempts"] == 0
        assert meta["errors"]["consecutive"] == 2
        assert meta["errors"]["total"] == 2
        assert engine.store.data["active_operation"] == journal
