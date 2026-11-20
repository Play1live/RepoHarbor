import json
from argparse import Namespace
from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import git

from repoharbor import status
from repoharbor.config import Retry, Source
from repoharbor.errors import GitError, Result
from repoharbor.github import DiscoveryError
from repoharbor.recovery import recover


def schedule(engine, monkeypatch, alias, *, existing=False):
    clock = Mock(return_value=1000)
    engine.retries.clock = clock
    with monkeypatch.context() as patch:
        patch.setattr(
            engine.git,
            "remote_refs" if existing else "clone",
            Mock(side_effect=GitError("GIT_NETWORK")),
        )
        assert engine.run(repository=alias) == Result.BACKUP_FAILED
    return clock


@pytest.mark.parametrize("provider_fixture", ["github_env", "gitlab_env"])
@pytest.mark.parametrize(
    "mirror", ["established", "new", "unproven", "conflict", "excluded", "corrupt"]
)
def test_manual_retry_applies_common_eligibility_on_failed_discovery(
    request, monkeypatch, remote, provider_fixture, mirror
):
    env = request.getfixturevalue(provider_fixture)
    host = env.key.split(":")[1]
    url = f"https://{host}/fictional/project.git"
    source = Source("demo", url)
    config = replace(env.config, auto_retry=True, sources=(source,))
    with env.create(config, git_instance=env.git()) as engine:
        established = mirror in ("established", "conflict", "excluded", "corrupt")
        if established:
            assert engine.run(repository=source.alias) == Result.SUCCESS
        clock = schedule(engine, monkeypatch, source.alias, existing=established)
        local = engine.path(source.alias) / "repository.git"
        if mirror == "unproven":
            git("clone", "--mirror", remote[1], local)
        if mirror == "conflict":
            assert engine.repos[env.key]["repository"]["clone_url"] == url
            git("clone", "--mirror", remote[1], engine.path(env.key) / "repository.git")
        if mirror == "excluded":
            engine.repos[source.alias]["selection"]["status"] = "excluded"
        if mirror == "corrupt":
            oid = git("rev-parse", "HEAD:hello.txt", cwd=local)
            (local / "objects" / oid[:2] / oid[2:]).unlink()
        env.client.discover.side_effect = DiscoveryError(
            "GITHUB_HTTP_ERROR", temporary=True
        )
        remote_refs = Mock(wraps=engine.git.remote_refs)
        monkeypatch.setattr(engine.git, "remote_refs", remote_refs)
        clock.return_value = 1300
        engine.retries.execute(source.alias)
        meta = engine.repos[source.alias]
        state = meta["retry"]
        if mirror == "established":
            remote_refs.assert_called_once_with(url, None)
            assert state["status"] == "succeeded" and state["attempts"] == 0
            assert meta["backup"]["successful_updates"] == 1
        else:
            remote_refs.assert_not_called()
            assert (
                state["block_reason"]
                == {
                    "new": "DISCOVERY_TEMPORARY",
                    "unproven": "DISCOVERY_TEMPORARY",
                    "conflict": "NOT_SELECTED",
                    "excluded": "NOT_SELECTED",
                    "corrupt": "RECOVERY_REQUIRED",
                }[mirror]
            )
            assert state["attempts"] == 0


@pytest.mark.parametrize("fixture_name", ["github_env", "gitlab_env"])
def test_discovery_deferrals_are_bounded_across_existing_git_attempts(
    request, monkeypatch, fixture_name
):
    env = request.getfixturevalue(fixture_name)
    config = replace(env.config, auto_retry=True, retry=Retry(3, 5, 12))
    with env.create(config, git_instance=env.git()) as engine:
        clock = schedule(engine, monkeypatch, env.key)
        normal_discover = env.client.discover.side_effect
        clone = Mock(side_effect=GitError("GIT_TIMEOUT"))
        monkeypatch.setattr(engine.git, "clone", clone)
        clock.return_value = 1005
        engine.retries.execute(env.key)
        clone.assert_called_once()
        env.client.discover.side_effect = DiscoveryError(
            "GITHUB_HTTP_ERROR", temporary=True
        )
        for count, (now, due) in enumerate(
            [(1015, 1020), (1020, 1030), (1030, 1042), (1042, None)], 1
        ):
            clock.return_value = now
            assert engine.retries.due() == env.key
            engine.retries.execute(env.key)
            retry = engine.repos[env.key]["retry"]
            assert retry["attempts"] == 1
            assert retry["last_attempt_at"] == 1005
            assert retry["last_error_code"] == "GIT_TIMEOUT"
            assert retry["discovery_deferrals"] == min(count, 3)
            assert retry["next_attempt_at"] == due
            assert engine.retries.due() is None
        assert retry["status"] == "blocked"
        assert retry["block_reason"] == "DISCOVERY_LIMIT_REACHED"
        env.client.discover.side_effect = normal_discover
        assert engine.run(repository=env.key) == Result.BACKUP_FAILED
        assert engine.repos[env.key]["retry"]["status"] == "blocked"
        assert clone.call_count == 2


def test_discovery_deferral_restarts_and_leaves_independent_manual_work_eligible(
    github_env, monkeypatch, capsys
):
    env = github_env
    independent = Source("demo", "https://example.org/repo.git")
    config = replace(env.config, auto_retry=True, sources=(independent,))
    original_discover = env.client.discover.side_effect
    with env.create(config, git_instance=env.git()) as engine:
        clock = schedule(engine, monkeypatch, env.key)
        assert engine.reconcile_operation()
        with monkeypatch.context() as patch:
            patch.setattr(
                engine.git, "clone", Mock(side_effect=GitError("GIT_TIMEOUT"))
            )
            assert engine.run(repository=independent.alias) == Result.BACKUP_FAILED
        env.client.discover.side_effect = DiscoveryError(
            "GITHUB_INVALID_RESPONSE_OR_NETWORK", temporary=True
        )
        clock.return_value = 1300
        engine.retries.execute(env.key)
        assert engine.retries.due() == independent.alias
        engine.retries.execute(independent.alias)
        assert engine.repos[independent.alias]["backup"]["successful_updates"] == 1
        args = Namespace(
            command="repositories", action="show", repository=env.key, json=True
        )
        assert status.print_status(config, args) == Result.SUCCESS
        view = json.loads(capsys.readouterr().out)["retry"]
        assert view["discovery_deferred"] and view["pending"]
        assert view["discovery_deferrals"] == 1
        assert view["max_discovery_deferrals"] == 3
        assert view["next_discovery_check_at"] == view["next_attempt_at"]
        assert view["attempts"] == 0 and view["block_reason"] == "DISCOVERY_TEMPORARY"
        args.json = False
        assert status.print_status(config, args) == Result.SUCCESS
        assert "discovery deferrals  1" in capsys.readouterr().out
    with env.create(config, git_instance=env.git()) as engine:
        recover(engine)
        engine.retries.clock = lambda: 1599
        assert engine.retries.due() is None
        engine.retries.clock = lambda: 10000  # Missed checks are never replayed.
        env.client.discover.side_effect = original_discover
        assert engine.retries.due() == env.key
        engine.retries.execute(env.key)
        assert engine.retries.due() is None
        meta = engine.repos[env.key]
        assert meta["retry"]["last_result"] == "succeeded"
        assert "discovery_deferrals" not in meta["retry"]
        assert meta["backup"]["successful_updates"] == 1


def test_retry_respects_cooldown_for_established_manual_mirror(github_env, monkeypatch):
    env = github_env
    source = Source("demo", "https://github.com/fictional/project.git")
    config = replace(env.config, auto_retry=True, sources=(source,))
    with env.create(config, git_instance=env.git()) as engine:
        assert engine.run(repository=source.alias) == Result.SUCCESS
        clock = schedule(engine, monkeypatch, source.alias, existing=True)
        remote_refs = Mock(wraps=engine.git.remote_refs)
        monkeypatch.setattr(engine.git, "remote_refs", remote_refs)
        clock.return_value = 1300
        engine.store.data["discovery"]["retry_at"] = 2000
        env.client.discover.reset_mock()
        engine.retries.execute(source.alias)
        env.client.discover.assert_not_called()
        remote_refs.assert_not_called()
        state = engine.repos[source.alias]["retry"]
        assert state["block_reason"] == "PROVIDER_COOLDOWN"
        assert state["next_attempt_at"] == 2000
        assert state["discovery_deferrals"] == state["attempts"] == 0

        # An API check that receives another cooldown consumes an actual deferral.
        clock.return_value = 2000
        env.client.discover.side_effect = DiscoveryError("GITHUB_RATE_LIMIT", 3000)
        engine.retries.execute(source.alias)
        remote_refs.assert_not_called()
        assert state["next_attempt_at"] == 3000
        assert state["discovery_deferrals"] == 1 and state["attempts"] == 0


def test_identity_conflict_error_blocks_established_manual_retry(
    github_env, monkeypatch
):
    env = github_env
    source = Source("demo", "https://github.com/fictional/project.git")
    config = replace(env.config, auto_retry=True, sources=(source,))
    with env.create(config, git_instance=env.git()) as engine:
        assert engine.run(repository=source.alias) == Result.SUCCESS
        clock = schedule(engine, monkeypatch, source.alias, existing=True)
        env.client.discover.side_effect = DiscoveryError("GITHUB_ALIAS_CONFLICT")
        clock.return_value = 1300
        engine.retries.execute(source.alias)
        retry = engine.repos[source.alias]["retry"]
        assert retry["block_reason"] == "DISCOVERY_FAILED"
        assert retry["attempts"] == 0
        backup = engine.repos[source.alias]["backup"]
        assert backup["successful_updates"] == 1


def test_newly_open_repair_journal_blocks_postponed_retry(github_env, monkeypatch):
    env = github_env
    config = replace(env.config, auto_retry=True)
    with env.create(config, git_instance=env.git()) as engine:
        clock = schedule(engine, monkeypatch, env.key)
        env.client.discover.side_effect = DiscoveryError(
            "GITHUB_HTTP_ERROR", temporary=True
        )
        clock.return_value = 1300
        engine.retries.execute(env.key)
        operation = engine.store.begin(env.key, "repair")
        clock.return_value = 1600
        env.client.discover.reset_mock()
        engine.retries.execute(env.key)
        env.client.discover.assert_not_called()
        assert engine.store.data["active_operation"]["id"] == operation
        retry = engine.repos[env.key]["retry"]
        assert retry["block_reason"] == "RECOVERY_REQUIRED"
        assert retry["attempts"] == 0 and retry["discovery_deferrals"] == 1


@pytest.mark.parametrize("code", ["GITHUB_UNAUTHORIZED", "GITHUB_INVALID_REPOSITORY"])
def test_permanent_discovery_failure_cannot_reopen_on_new_git_failure(
    github_env, monkeypatch, code
):
    env = github_env
    with env.create(
        replace(env.config, auto_retry=True), git_instance=env.git()
    ) as engine:
        original_discover = env.client.discover.side_effect
        clock = schedule(engine, monkeypatch, env.key)
        env.client.discover.side_effect = DiscoveryError(code)
        clock.return_value = 1300
        engine.retries.execute(env.key)
        env.client.discover.side_effect = original_discover
        schedule(engine, monkeypatch, env.key)
        assert engine.repos[env.key]["retry"]["block_reason"] == "DISCOVERY_FAILED"
        assert engine.retries.due() is None


def test_other_gitlab_port_can_retry_while_first_authority_is_deferred(
    gitlab_env, monkeypatch
):
    from conftest import raw_project

    from repoharbor.config import Provider
    from repoharbor.gitlab import parse_project
    from repoharbor.providers import Observation

    env = gitlab_env
    other = Provider("other", "third", type="gitlab", host="gitlab.example.org:8443")
    other_key = f"gitlab:{other.host}:18"
    config = replace(
        env.config,
        auto_retry=True,
        providers=(*env.config.providers, other),
        credentials=(*env.config.credentials, other.credential),
    )
    normal_discover = env.client.discover.side_effect
    blocked = False

    def discover(provider):
        if provider == other:
            return [
                Observation(
                    parse_project(raw_project(18, host=other.host), other.host),
                    other.credential,
                    True,
                )
            ]
        if blocked:
            raise DiscoveryError("GITLAB_HTTP_ERROR", temporary=True)
        return normal_discover(provider)

    env.client.discover.side_effect = discover
    with env.create(config, git_instance=env.git()) as engine:
        engine.git.credentials.values[other.credential] = "fictional-third"
        clock = schedule(engine, monkeypatch, env.key)
        assert engine.reconcile_operation()
        schedule(engine, monkeypatch, other_key)
        assert engine.reconcile_operation()
        clone = engine.git.clone

        def mapped_clone(url, *args):
            return clone(url.replace(":8443", ""), *args)

        monkeypatch.setattr(engine.git, "clone", mapped_clone)
        blocked = True
        engine.retries.clock = clock
        clock.return_value = 1300
        engine.retries.execute(env.key)
        assert engine.repos[env.key]["retry"]["next_attempt_at"] == 1600
        assert engine.retries.due() == other_key
        engine.retries.execute(other_key)
        assert engine.repos[other_key]["backup"]["successful_updates"] == 1
        assert engine.repos[env.key]["retry"]["discovery_deferrals"] == 1


def test_expired_retry_after_does_not_enable_manual_fallback(github_env, monkeypatch):
    env = github_env
    source = Source("demo", "https://github.com/fictional/project.git")
    config = replace(env.config, auto_retry=True, sources=(source,))
    with env.create(config, git_instance=env.git()) as engine:
        assert engine.run(repository=source.alias) == Result.SUCCESS
        clock = schedule(engine, monkeypatch, source.alias, existing=True)
        remote_refs = Mock(wraps=engine.git.remote_refs)
        monkeypatch.setattr(engine.git, "remote_refs", remote_refs)
        clock.return_value = 1300
        env.client.discover.side_effect = DiscoveryError("GITHUB_RATE_LIMIT", 1300)
        engine.retries.execute(source.alias)
        remote_refs.assert_not_called()
        state = engine.repos[source.alias]["retry"]
        assert state["block_reason"] == "PROVIDER_COOLDOWN"
        assert state["next_attempt_at"] == 1600
        assert state["discovery_deferrals"] == 1 and state["attempts"] == 0


def test_new_explicit_exclusion_is_applied_before_discovery(github_env, monkeypatch):
    env = github_env
    config = replace(env.config, auto_retry=True)
    with env.create(config, git_instance=env.git()) as engine:
        schedule(engine, monkeypatch, env.key)
    config = replace(config, selection=replace(config.selection, excluded=(env.key,)))
    with env.create(config, git_instance=env.git()) as engine:
        recover(engine)
        engine.retries.clock = lambda: 1300
        env.client.discover.reset_mock()
        engine.retries.execute(env.key)
        env.client.discover.assert_not_called()
        assert engine.repos[env.key]["retry"]["block_reason"] == "NOT_SELECTED"
        assert not engine.git.calls


def test_git_retry_failure_keeps_discovery_deferral_budget(github_env, monkeypatch):
    env = github_env
    config = replace(env.config, auto_retry=True, retry=Retry(3, 5, 12))
    with env.create(config, git_instance=env.git()) as engine:
        clock = schedule(engine, monkeypatch, env.key)
        original_discover = env.client.discover.side_effect
        temporary = DiscoveryError("GITHUB_HTTP_ERROR", temporary=True)
        env.client.discover.side_effect = temporary
        clock.return_value = 1005
        engine.retries.execute(env.key)
        env.client.discover.side_effect = original_discover
        clone = Mock(side_effect=GitError("GIT_TIMEOUT"))
        monkeypatch.setattr(engine.git, "clone", clone)
        clock.return_value = 1010
        engine.retries.execute(env.key)
        clone.assert_called_once()
        state = engine.repos[env.key]["retry"]
        assert state["next_attempt_at"] == 1020
        assert state["attempts"] == state["discovery_deferrals"] == 1
        env.client.discover.side_effect = temporary
        clock.return_value = 1020
        engine.retries.execute(env.key)
        assert state["next_attempt_at"] == 1030
        assert state["attempts"] == 1 and state["discovery_deferrals"] == 2
        clone.assert_called_once()
