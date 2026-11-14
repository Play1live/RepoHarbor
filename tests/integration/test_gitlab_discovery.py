import copy
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import api_response, git, raw_project, raw_repo

from repoharbor.config import Discovery, Provider, Source, Storage
from repoharbor.errors import HarborError
from repoharbor.github import DiscoveryError, parse_repository
from repoharbor.gitlab import GitLab
from repoharbor.persistence import atomic_json, read_json
from repoharbor.providers import Observation
from repoharbor.recovery import recover
from repoharbor.selection import Selection


def test_mirrors_deduplicate_update_rename_exclude_restart_and_restore(
    gitlab_env, remote, tmp_path
):
    env = gitlab_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        mirror = engine.path(env.key) / "repository.git"
        inode = mirror.stat().st_ino
        assert len(list(env.config.root.rglob("repository.git"))) == 1
        assert engine.repos[env.key]["backup"]["successful_updates"] == 1
        engine.run()
        assert engine.repos[env.key]["backup"]["status"] == "up_to_date"
        (remote[0] / "hello.txt").write_text("second\n")
        git("commit", "-am", "second", cwd=remote[0])
        git("tag", "example-tag", cwd=remote[0])
        git("push", "origin", "main", "--tags", cwd=remote[0])
        env.results["first"] = env.results["second"] = [
            raw_project(owner="group/sub/deep", kind="group", name="renamed"),
        ]
        engine.run()
        assert mirror.stat().st_ino == inode
        assert engine.repos[env.key]["backup"]["successful_updates"] == 2
        assert len(engine.repos[env.key]["repository"]["aliases"]) == 2
        assert "refs/tags/example-tag" in engine.git.refs(mirror)
    cfg = replace(env.config, selection=Selection(mode="all", excluded=(env.key,)))
    with env.create(cfg, git_instance=env.git()) as engine:
        recover(engine)
        assert not engine.git.calls
        engine.run()
        assert engine.sources == ()
        assert mirror.stat().st_ino == inode
        assert not any("fetch" in c or "ls-remote" in c for c in engine.git.calls)
    git("clone", "--no-hardlinks", mirror, tmp_path / "restored")
    assert (tmp_path / "restored/hello.txt").read_text() == "second\n"


def test_gitlab_failure_keeps_manual_github_and_another_instance_running(
    gitlab_env, monkeypatch
):
    env = gitlab_env
    cfg = replace(
        env.config,
        sources=(Source("demo", "https://example.org/repo.git"),),
        providers=env.config.providers
        + (
            Provider("gh", "first"),
            Provider("other", "second", type="gitlab", host="other.example.org"),
        ),
    )
    github = Mock(retry_at=0.0)
    github.discover.return_value = [
        Observation(parse_repository(raw_repo()), "first", True)
    ]
    monkeypatch.setattr("repoharbor.engine.GitHub", lambda *a, **kw: github)
    other = Mock(retry_at=0.0)
    from repoharbor.gitlab import parse_project

    other.discover.return_value = [
        Observation(
            parse_project(raw_project(host="other.example.org"), "other.example.org"),
            "second",
            True,
        )
    ]
    clients = iter([env.client, other])
    monkeypatch.setattr("repoharbor.engine.GitLab", lambda *a, **kw: next(clients))
    env.client.discover.side_effect = DiscoveryError(
        "GITLAB_RATE_LIMIT", time.time() + 3600
    )
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.discover()
        assert {s.alias for s in engine.sources} == {
            "demo",
            "github:github.com:17",
            "gitlab:other.example.org:17",
        }
        assert engine.store.data["discovery"]["state"] == "completed"
        states = engine.store.data["provider_discovery"]
        assert states["gitlab:gitlab.example.org"]["state"] == "failed"
        assert states["gitlab:other.example.org"]["state"] == "completed"


@pytest.mark.parametrize(
    "error",
    [
        "UNAUTHORIZED",
        "FORBIDDEN",
        "NOT_ACCESSIBLE",
        "HTTP_ERROR",
        "INCOMPLETE_PAGINATION",
    ],
)
def test_failed_token_is_transactional_and_preserves_existing(gitlab_env, error):
    env = gitlab_env
    cfg = replace(env.config, sources=(Source("demo", "https://example.org/repo.git"),))
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        before = copy.deepcopy(engine.repos[env.key])
        checked = engine.repos["demo"]["backup"]["last_checked_at"]
        env.client.discover.side_effect = [
            [
                Observation(
                    env.client.discover.side_effect(cfg.providers[0])[0].repository,
                    "first",
                    True,
                )
            ],
            DiscoveryError("GITLAB_" + error),
        ]
        engine.run()
        assert engine.repos[env.key] == before
        assert engine.repos["demo"]["backup"]["last_checked_at"] != checked
        assert engine.sources == cfg.sources


def test_actual_discovery_transport_and_restart_cooldown(gitlab_env, monkeypatch):
    env = gitlab_env
    cfg = replace(
        env.config, providers=env.config.providers[:1], discovery=Discovery(0)
    )
    monkeypatch.setattr("repoharbor.engine.GitLab", GitLab)
    opener = Mock()
    opener.open.side_effect = [
        api_response({"id": 1, "username": "fictional"}),
        api_response([raw_project()]),
        api_response([raw_project()]),
        api_response([], {"RateLimit-Remaining": "0", "Retry-After": "172800"}),
    ]
    monkeypatch.setattr("repoharbor.github.build_opener", lambda *a: opener)
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        assert engine.repos[env.key]["backup"]["successful_updates"] == 1
        until = engine.store.data["provider_discovery"]["gitlab:gitlab.example.org"][
            "retry_at"
        ]
        assert until > time.time() + 86400
    opener.reset_mock()
    with env.create(cfg, git_instance=env.git()) as engine:
        recover(engine)
        engine.run()
        opener.open.assert_not_called()
        assert not engine.git.calls
        assert engine.sources == ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "foreign.example.org"),
        ("id", "18"),
        ("clone_url", "https://foreign.example.org/a.git"),
        ("aliases", ["bad"]),
        ("visibility", "bad"),
    ],
)
def test_corrupt_metadata_fails_closed(gitlab_env, field, value):
    env = gitlab_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        path = engine.path(env.key) / "metadaten.json"
        data = read_json(path)
        data["repository"][field] = value
        atomic_json(path, data)
    before = path.read_bytes()
    with pytest.raises(HarborError), env.create(env.config, git_instance=env.git()):
        pass
    assert path.read_bytes() == before


def test_manual_conflict_limits_removed_provider(gitlab_env):
    env = gitlab_env
    cfg = replace(
        env.config,
        sources=(Source("manual", "https://gitlab.example.org/fictional/project.git"),),
    )
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        assert (
            engine.repos[env.key]["selection"]["reason"] == "manual_identity_conflict"
        )
        assert len(list(cfg.root.rglob("repository.git"))) == 1
    with env.create(
        replace(env.config, providers=()), git_instance=env.git()
    ) as engine:
        engine.run()
        assert (
            engine.repos[env.key]["selection"]["reason"] == "manual_identity_conflict"
        )
    cfg = replace(
        env.config,
        sources=(),
        storage=Storage(
            overrides={env.key: 0},
            min_free_bytes=0,
        ),
    )
    # Existing manual identity remains a conflict even after config removal.
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        assert engine.sources == ()
