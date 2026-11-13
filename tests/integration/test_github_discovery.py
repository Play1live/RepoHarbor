import copy
import errno
import time
from dataclasses import replace

import pytest
from conftest import git, raw_repo

from repoharbor.config import Source, Storage
from repoharbor.errors import HarborError
from repoharbor.github import DiscoveryError, parse_repository
from repoharbor.persistence import atomic_json, read_json
from repoharbor.providers import Observation
from repoharbor.recovery import recover
from repoharbor.selection import Selection


def test_deduplicated_mirror_rename_update_restart_and_offline_restore(
    github_env, remote, tmp_path
):
    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        mirror = engine.path(env.key) / "repository.git"
        inode = mirror.stat().st_ino
        initial = copy.deepcopy(engine.repos[env.key])
        assert initial["backup"]["successful_updates"] == 1
        assert initial["authentication"]["credential_name"] == "first"
        assert len(list(env.config.root.rglob("repository.git"))) == 1
        env.results["first"] = env.results["second"] = [
            raw_repo(owner="fictional-org", name="renamed")
        ]
        (remote[0] / "hello.txt").write_text("renamed\n")
        git("commit", "-am", "rename", cwd=remote[0])
        git("push", "origin", "main", cwd=remote[0])
        engine.run()
        assert mirror.stat().st_ino == inode
        assert engine.repos[env.key]["backup"]["successful_updates"] == 2
        assert engine.repos[env.key]["repository"]["owner"] == "fictional-org"
    with env.create(env.config, git_instance=env.git()) as engine:
        recover(engine)
        assert not any("clone" in c or "fetch" in c for c in engine.git.calls)
        engine.run()
        assert engine.repos[env.key]["backup"]["successful_updates"] == 2
        assert engine.repos[env.key]["backup"]["status"] == "up_to_date"
        assert engine.path(env.key) / "repository.git" == mirror
    git("clone", "--no-hardlinks", mirror, tmp_path / "restored")
    assert (tmp_path / "restored/hello.txt").read_text() == "renamed\n"


def test_new_excluded_unavailable_and_reincluded_keep_mirror(github_env):
    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        mirror = engine.path(env.key) / "repository.git"
        inode = mirror.stat().st_ino
        env.results["first"].append(raw_repo(18, name="new"))
        engine.config = replace(
            env.config, selection=Selection(mode="all_except", excluded=(env.key,))
        )
        engine.run()
        assert engine.repos[env.key]["selection"]["status"] == "excluded"
        assert engine.repos[env.key]["backup"]["successful_updates"] == 1
        assert (engine.path("github:github.com:18") / "repository.git").exists()
        assert mirror.stat().st_ino == inode
        env.results["first"] = env.results["second"] = []
        engine.config = env.config
        before = copy.deepcopy(engine.repos[env.key]["backup"])
        engine.run()
        assert engine.repos[env.key]["source"]["status"] == "unavailable"
        assert engine.repos[env.key]["backup"] == before
        env.results["second"] = [raw_repo()]
        engine.run()
        assert engine.repos[env.key]["backup"]["status"] == "up_to_date"
        assert mirror.stat().st_ino == inode


def test_failure_of_second_token_preserves_all_selection_and_runs_manual(
    github_env, caplog
):
    env = github_env
    cfg = replace(env.config, sources=(Source("demo", "https://example.org/repo.git"),))
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        before = copy.deepcopy(engine.repos[env.key])
        manual_checks = engine.repos["demo"]["backup"]["last_checked_at"]
        env.client.discover.side_effect = [
            [env.client.discover.side_effect(cfg.providers[0])[0]],
            DiscoveryError("GITHUB_INVALID_PAGE"),
        ]
        engine.run()
        assert engine.repos[env.key] == before
        assert engine.repos["demo"]["backup"]["last_checked_at"] != manual_checks
        assert engine.sources == cfg.sources
        assert engine.store.data["discovery"]["state"] == "failed"
        assert "GITHUB_INVALID_PAGE" in caplog.text


def test_rate_limit_persisted_cooldown_and_provider_removal(github_env):
    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        env.client.discover.side_effect = DiscoveryError(
            "GITHUB_RATE_LIMIT", time.time() + 3600
        )
        engine.run()
        assert engine.store.data["discovery"]["retry_at"] > time.time()
    env.client.discover.reset_mock()
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        env.client.discover.assert_not_called()
        assert engine.sources == ()
        assert engine.repos[env.key]["selection"]["status"] == "included"
    with env.create(
        replace(env.config, providers=()), git_instance=env.git()
    ) as engine:
        engine.run()
        assert engine.repos[env.key]["selection"]["reason"] == "provider_disabled"
        assert (engine.path(env.key) / "repository.git").exists()


def test_manual_existing_mirror_never_adopted_or_duplicated(github_env):
    env = github_env
    cfg = replace(
        env.config,
        sources=(Source("manual", "https://github.com/fictional/project.git"),),
    )
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        manual = engine.path("manual") / "repository.git"
        inode = manual.stat().st_ino
        assert (
            engine.repos[env.key]["selection"]["reason"] == "manual_identity_conflict"
        )
        assert not (engine.path(env.key) / "repository.git").exists()
        assert engine.repos["manual"]["repository"]["provider"] == "manual"
        engine.run()
        assert manual.stat().st_ino == inode
        assert len(list(cfg.root.rglob("repository.git"))) == 1


def test_new_manual_entry_cannot_duplicate_provider_mirror(github_env):
    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        mirror = engine.path(env.key) / "repository.git"
    cfg = replace(
        env.config,
        sources=(Source("manual", "https://github.com/fictional/project.git"),),
    )
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        assert mirror.exists()
        assert not (engine.path("manual") / "repository.git").exists()
        assert engine.sources == ()
        assert (
            engine.repos[env.key]["selection"]["reason"] == "manual_identity_conflict"
        )


def test_provider_limits_and_no_startup_remote_repair(github_env, remote):
    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        mirror = engine.path(env.key) / "repository.git"
        oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
        (mirror / "objects" / oid[:2] / oid[2:]).unlink()
        engine.repos[env.key]["integrity"]["status"] = "suspect"
        engine.save_meta(env.key)
    with env.create(env.config, git_instance=env.git()) as engine:
        recover(engine)
        assert (
            engine.repos[env.key]["backup"]["repair_deferred"] == "DISCOVERY_REQUIRED"
        )
        assert engine.repos[env.key]["integrity"]["repair_attempts"] == 0
        assert not any("fetch" in c for c in engine.git.calls)
        env.client.discover.side_effect = DiscoveryError("GITHUB_UNAUTHORIZED")
        engine.run()
        assert not any("fetch" in c for c in engine.git.calls)
        env.client.discover.side_effect = lambda p: [
            Observation(parse_repository(raw_repo()), p.credential, True)
        ]
        engine.run()
        assert engine.git.healthy(mirror)
        assert engine.repos[env.key]["integrity"]["successful_repairs"] == 1
    cfg = replace(env.config, storage=Storage(overrides={env.key: 0}, min_free_bytes=0))
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        assert engine.repos[env.key]["backup"]["status"] == "repo_limit_reached"
        assert not any("ls-remote" in c or "fetch" in c for c in engine.git.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "18"),
        ("host", "evil.example.org"),
        ("aliases", ["bad"]),
        ("clone_url", "https://evil.example.org/repo.git"),
    ],
)
def test_invalid_provider_metadata_fails_closed_without_overwrite(
    github_env, field, value
):
    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        path = engine.path(env.key) / "metadaten.json"
        meta = read_json(path)
        meta["repository"][field] = value
        atomic_json(path, meta)
    before = path.read_bytes()
    with pytest.raises(HarborError), env.create(env.config, git_instance=env.git()):
        pass
    assert path.read_bytes() == before


def test_discovery_publication_write_failure_leaves_no_runnable_provider(
    github_env, monkeypatch
):
    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:
        original = engine.save_meta

        def fail(alias):
            original(alias)
            raise OSError(errno.ENOSPC, "injected")

        monkeypatch.setattr(engine, "save_meta", fail)
        with pytest.raises(OSError):
            engine.run()
        assert engine.sources == ()
        assert engine.store.data["discovery"]["state"] == "running"
        assert not list(env.config.root.rglob("repository.git"))
    with env.create(env.config, git_instance=env.git()) as engine:
        recover(engine)
        assert not engine.git.calls
        engine.run()
        assert engine.repos[env.key]["backup"]["successful_updates"] == 1
