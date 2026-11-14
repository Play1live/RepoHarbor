import copy
import time
from dataclasses import replace
from unittest.mock import Mock, call

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


@pytest.mark.parametrize("token_count", [2, 3])
def test_manual_identity_404_uses_later_instance_token_without_duplicate_mirror(
    gitlab_env, token_count
):
    env = gitlab_env
    host = "gitlab.example.org"
    names = ("first", "second", "third")[:token_count]
    cfg = replace(
        env.config,
        sources=(Source("manual", f"https://{host}/group/beta.git"),),
        credentials=names,
        providers=tuple(
            Provider(f"provider_{name}", name, type="gitlab", host=host)
            for name in names
        ),
    )
    alpha = raw_project(17, owner="group", name="alpha", kind="group")
    beta = raw_project(18, owner="group", name="beta", kind="group")
    env.results.update({name: [alpha] for name in names})
    env.results[names[-1]] = [alpha, beta]
    key = f"gitlab:{host}:18"
    env.client.manual_identity.side_effect = [
        *(DiscoveryError("GITLAB_NOT_ACCESSIBLE") for _ in names[:-1]),
        key,
    ]
    transport = env.git()
    transport.credentials.values["third"] = "fictional-third"
    with env.create(cfg, git_instance=transport) as engine:
        engine.run()
        assert (
            engine.store.data["provider_discovery"][f"gitlab:{host}"]["state"]
            == "completed"
        )
        assert env.client.manual_identity.call_args_list == [
            call(f"{host}/group/beta", name) for name in names
        ]
        assert engine.repos[key]["selection"]["reason"] == "manual_identity_conflict"
        assert not (engine.path(key) / "repository.git").exists()
        assert (engine.path(env.key) / "repository.git").exists()
        manual = engine.path("manual") / "repository.git"
        assert engine.git.healthy(manual)
        assert len(list(cfg.root.rglob("repository.git"))) == 2
        calls = [c[0] for c in env.client.method_calls]
        assert max(i for i, c in enumerate(calls) if c == "discover") < min(
            i for i, c in enumerate(calls) if c == "manual_identity"
        )


@pytest.mark.parametrize(
    "code",
    [
        "GITLAB_NOT_ACCESSIBLE",
        "GITLAB_UNAUTHORIZED",
        "GITLAB_FORBIDDEN",
        "GITLAB_RATE_LIMIT",
        "GITLAB_INVALID_RESPONSE_OR_NETWORK",
        "GITLAB_HTTP_ERROR",
        "GITLAB_INVALID_PROJECT",
        "GITLAB_INCOMPLETE_PAGINATION",
    ],
)
def test_manual_resolution_failure_preserves_catalog_and_independent_backups(
    gitlab_env, code, caplog
):
    env = gitlab_env
    host = "gitlab.example.org"
    state_key = "gitlab:" + host
    cfg = replace(
        env.config,
        sources=(
            Source("manual", f"https://{host}/group/beta.git"),
            Source("demo", "https://example.org/repo.git"),
        ),
    )
    alpha = raw_project(17, owner="group", name="alpha", kind="group")
    beta = raw_project(18, owner="group", name="beta", kind="group")
    env.results["first"] = env.results["second"] = [alpha, beta]
    env.client.manual_identity.return_value = f"gitlab:{host}:18"
    until = time.time() + 3600 if code == "GITLAB_RATE_LIMIT" else 0
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        before = {
            key: (engine.path(key) / "metadaten.json").read_bytes()
            for key in engine.repos
            if key.startswith("gitlab:")
        }
        mirrors = {p: p.stat().st_ino for p in cfg.root.rglob("repository.git")}
        checked = engine.repos["manual"]["backup"]["last_checked_at"]
        env.client.manual_identity.reset_mock()
        env.client.manual_identity.side_effect = DiscoveryError(code, until)
        engine.git.calls.clear()
        engine.run()
        state = engine.store.data["provider_discovery"][state_key]
        assert state["state"] == "failed"
        expected = (
            "GITLAB_MANUAL_IDENTITY_UNRESOLVED"
            if code == "GITLAB_NOT_ACCESSIBLE"
            else code
        )
        assert state["code"] == expected
        assert state["retry_at"] == until
        assert env.client.manual_identity.call_args_list == [
            call(f"{host}/group/beta", credential)
            for credential in (
                ("first", "second") if code == "GITLAB_NOT_ACCESSIBLE" else ("first",)
            )
        ]
        assert all(
            (engine.path(key) / "metadaten.json").read_bytes() == contents
            for key, contents in before.items()
        )
        assert mirrors == {p: p.stat().st_ino for p in cfg.root.rglob("repository.git")}
        assert engine.sources == cfg.sources
        assert engine.repos["manual"]["backup"]["last_checked_at"] != checked
        assert engine.repos["demo"]["backup"]["status"] == "up_to_date"
        assert not any("clone" in args or "fetch" in args for args in engine.git.calls)
        assert "fictional-first" not in caplog.text
        assert "fictional-second" not in caplog.text
    if until:
        env.client.reset_mock()
        with env.create(cfg, git_instance=env.git()) as engine:
            engine.run()
            env.client.discover.assert_not_called()
            env.client.manual_identity.assert_not_called()
            assert (
                engine.store.data["provider_discovery"][state_key]["retry_at"] == until
            )
            assert engine.sources == cfg.sources


def test_manual_resolution_after_all_discoveries_and_first_success_stops(gitlab_env):
    env = gitlab_env
    host = "gitlab.example.org"
    cfg = replace(
        env.config, sources=(Source("manual", f"https://{host}/fictional/project.git"),)
    )
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        env.client.manual_identity.assert_called_once_with(
            f"{host}/fictional/project", "first"
        )
        assert len(list(cfg.root.rglob("repository.git"))) == 1
        assert (
            engine.repos[env.key]["selection"]["reason"] == "manual_identity_conflict"
        )
        env.client.reset_mock()
        env.client.discover.side_effect = [
            [],
            DiscoveryError("GITLAB_INCOMPLETE_PAGINATION"),
        ]
        engine.run()
        env.client.manual_identity.assert_not_called()
        assert engine.store.data["provider_discovery"][f"gitlab:{host}"]["code"] == (
            "GITLAB_INCOMPLETE_PAGINATION"
        )


def test_manual_resolution_multiple_addresses_and_renamed_identity_conflict(gitlab_env):
    env = gitlab_env
    host = "gitlab.example.org"
    alpha = raw_project(17, owner="group", name="alpha", kind="group")
    beta = raw_project(18, owner="group", name="beta", kind="group")
    cfg = replace(
        env.config,
        sources=(
            Source("beta", f"https://{host}/group/beta.git"),
            Source("old_alpha", f"https://{host}/former/alpha.git"),
        ),
    )
    env.results["first"] = env.results["second"] = [alpha, beta]

    def resolve(name, credential):
        if credential == "first":
            raise DiscoveryError("GITLAB_NOT_ACCESSIBLE")
        return f"gitlab:{host}:" + ("17" if name.endswith("/alpha") else "18")

    env.client.manual_identity.side_effect = resolve
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        assert env.client.manual_identity.call_args_list == [
            call(name, credential)
            for name in (f"{host}/former/alpha", f"{host}/group/beta")
            for credential in ("first", "second")
        ]
        assert len(list(cfg.root.rglob("repository.git"))) == 2
        for key in (env.key, f"gitlab:{host}:18"):
            assert (
                engine.repos[key]["selection"]["reason"] == "manual_identity_conflict"
            )
            assert not (engine.path(key) / "repository.git").exists()
        assert {s.alias for s in engine.sources} == {"beta", "old_alpha"}


def test_manual_404_exhaustion_does_not_block_other_hosts_or_github(
    gitlab_env, monkeypatch
):
    env = gitlab_env
    host = "gitlab.example.org"
    other_host = "other.example.org"
    cfg = replace(
        env.config,
        credentials=env.config.credentials + ("other", "github"),
        sources=(
            Source("manual", f"https://{host}/group/beta.git"),
            Source("other_manual", f"https://{other_host}/group/shared.git"),
        ),
        providers=env.config.providers
        + (
            Provider("other", "other", type="gitlab", host=other_host),
            Provider("github", "github"),
        ),
    )
    github = Mock(retry_at=0.0)
    github.discover.return_value = [
        Observation(parse_repository(raw_repo()), "github", True)
    ]
    monkeypatch.setattr("repoharbor.engine.GitHub", lambda *a, **kw: github)
    other = Mock(retry_at=0.0)
    other.discover.return_value = []
    other.manual_identity.return_value = f"gitlab:{other_host}:19"
    clients = iter((env.client, other))
    monkeypatch.setattr("repoharbor.engine.GitLab", lambda *a, **kw: next(clients))
    env.client.manual_identity.side_effect = DiscoveryError("GITLAB_NOT_ACCESSIBLE")
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.discover()
        assert env.client.manual_identity.call_args_list == [
            call(f"{host}/group/beta", "first"),
            call(f"{host}/group/beta", "second"),
        ]
        other.manual_identity.assert_called_once_with(
            f"{other_host}/group/shared", "other"
        )
        states = engine.store.data["provider_discovery"]
        assert states["gitlab:" + host]["state"] == "failed"
        assert states["gitlab:" + other_host]["state"] == "completed"
        assert engine.store.data["discovery"]["state"] == "completed"
        assert {s.alias for s in engine.sources} == {
            "manual",
            "other_manual",
            "github:github.com:17",
        }


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500])
def test_real_api_manual_lookup_only_retries_404(gitlab_env, monkeypatch, status):
    import io
    from urllib.error import HTTPError

    env = gitlab_env
    host = "gitlab.example.org"
    cfg = replace(
        env.config,
        sources=(Source("manual", f"https://{host}/fictional/project.git"),),
        discovery=Discovery(0),
    )
    client = GitLab(env.git().credentials, request_delay_ms=0)
    # Keep provider discovery isolated; manual lookups use the real transport/parser.
    client.discover = Mock(return_value=[])
    opener = Mock()
    first_error = HTTPError("", 404, "fictional-secret", {}, io.BytesIO())
    second = (
        api_response(raw_project())
        if status == 200
        else HTTPError(
            "",
            status,
            "fictional-secret",
            {"Retry-After": "3600"} if status == 429 else {},
            io.BytesIO(),
        )
    )
    opener.open.side_effect = [first_error, second]
    client.opener = opener
    monkeypatch.setattr("repoharbor.engine.GitLab", lambda *a, **kw: client)
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.discover()
        requests = [c.args[0] for c in opener.open.call_args_list]
        assert [r.get_header("Private-token") for r in requests] == [
            "fictional-first",
            "fictional-second",
        ]
        assert all(
            r.full_url.startswith(f"https://{host}/api/v4/projects/") for r in requests
        )
        state = engine.store.data["provider_discovery"]["gitlab:" + host]
        assert state["state"] == ("completed" if status == 200 else "failed")
        if status == 429:
            assert state["retry_at"] > time.time() + 3500
            engine.discover()
            assert opener.open.call_count == 2
