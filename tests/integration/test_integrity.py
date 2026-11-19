import threading
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import git, raw_project, raw_repo

from repoharbor import integrity
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError, Result
from repoharbor.git_backup import Git
from repoharbor.github import DiscoveryError, parse_repository
from repoharbor.gitlab import parse_project
from repoharbor.persistence import utcnow
from repoharbor.providers import Observation
from repoharbor.recovery import recover
from repoharbor.registry import plan


def snapshot(folder):
    return {
        p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    }


def blob_path(mirror):
    oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
    return mirror / "objects" / oid[:2] / oid[2:]


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
