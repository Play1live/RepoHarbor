import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from conftest import git

from repoharbor import cli, control
from repoharbor.config import Source, Storage
from repoharbor.errors import GitError, Result
from repoharbor.github import DiscoveryError
from repoharbor.scheduler import Cron, Scheduler
from repoharbor.selection import Selection


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["repoharbor", *args])
    return cli.main()


def test_manual_clone_check_fetch_and_restore(
    factory, remote, tmp_path, monkeypatch, capsys
):
    config = replace(
        factory.config,
        manual_run=True,
        sources=factory.config.sources
        + (Source("other", "https://example.org/other.git"),),
    )
    monkeypatch.setattr(cli, "load", lambda _: config)
    work, bare = remote
    with factory(config) as engine:
        real_run = engine.git.run

        def map_second(args, *a, **kw):
            return real_run(
                [
                    str(bare) if x == "https://example.org/other.git" else x
                    for x in args
                ],
                *a,
                **kw,
            )

        monkeypatch.setattr(engine.git, "run", map_second)
        scheduler = Scheduler(Cron(config.schedule), config.timezone, datetime.now(UTC))
        monitor = control.Control(engine, scheduler, engine.git.stop)
        with monitor.start():
            assert not (engine.path("demo") / "repository.git").exists()
            assert invoke(monkeypatch, "backup", "run", "--all") == Result.SUCCESS
            assert "completed successfully" in capsys.readouterr().out
            before = engine.store.data["scheduler"]["last_successful_run_at"]
            other = engine.path("other")
            other_data = (other / "metadaten.json").read_bytes()
            mirror = engine.path("demo") / "repository.git"
            first = engine.repos["demo"]["backup"]["last_updated_at"]
            assert invoke(monkeypatch, "backup", "run", "--repository", "demo") == 0
            assert engine.repos["demo"]["backup"]["last_updated_at"] == first
            (work / "hello.txt").write_text("manual update\n")
            git("commit", "-am", "update", cwd=work)
            git("tag", "demo-tag", cwd=work)
            git("push", "origin", "main", "--tags", cwd=work)
            assert invoke(monkeypatch, "backup", "run", "--repository", "demo") == 0
            assert engine.repos["demo"]["backup"]["successful_updates"] == 2
            assert (other / "metadaten.json").read_bytes() == other_data
            assert git("rev-parse", "HEAD", cwd=other / "repository.git") != git(
                "rev-parse", "HEAD", cwd=mirror
            )
            assert engine.store.data["scheduler"]["last_successful_run_at"] == before
            assert engine.store.data["active_run"]["trigger"] == "manual"
            assert engine.store.data["active_run"]["repository"] == "demo"
            assert invoke(monkeypatch, "backup", "run", "--repository", "missing") == 3
    git("clone", "--no-hardlinks", mirror, tmp_path / "restored")
    assert (tmp_path / "restored/hello.txt").read_text() == "manual update\n"
    assert git("tag", cwd=tmp_path / "restored") == "demo-tag"


@pytest.mark.parametrize("fixture", ["github_env", "gitlab_env"])
def test_manual_provider_fresh_discovery_and_filters(request, fixture):
    env = request.getfixturevalue(fixture)
    with env.create(env.config, git_instance=env.git()) as engine:
        assert engine.run(manual=True, repository=env.key) == Result.SUCCESS
        assert env.client.discover.call_count == 2
        checked = engine.repos[env.key]["backup"]["last_checked_at"]
        count = len(engine.git.calls)
        env.client.discover.side_effect = DiscoveryError("GITHUB_INVALID_PAGE")
        assert engine.run(manual=True, repository=env.key) == Result.DISCOVERY_FAILED
        assert len(engine.git.calls) == count
        assert engine.repos[env.key]["backup"]["last_checked_at"] == checked
        env.client.discover.side_effect = None
        env.client.discover.return_value = []
        assert engine.run(manual=True, repository=env.key) == Result.NOT_SELECTED
        assert engine.run(manual=True) == Result.NOT_SELECTED
        assert len(engine.git.calls) == count


@pytest.mark.parametrize("fixture", ["github_env", "gitlab_env"])
def test_manual_provider_explicit_exclusion(request, fixture):
    env = request.getfixturevalue(fixture)
    config = replace(env.config, selection=Selection(mode="all", excluded=(env.key,)))
    with env.create(config, git_instance=env.git()) as engine:
        assert engine.run(manual=True, repository=env.key) == Result.NOT_SELECTED
        assert engine.run(manual=True) == Result.NOT_SELECTED
        assert not engine.git.calls


@pytest.mark.parametrize(
    "storage",
    [
        Storage(min_free_bytes=0, global_bytes=0),
        Storage(min_free_bytes=2**80),
        Storage(min_free_bytes=0, repo_bytes=0),
    ],
)
def test_manual_capacity_checks(factory, storage):
    with factory(replace(factory.config, storage=storage)) as engine:
        assert engine.run(manual=True, repository="demo") == Result.BACKUP_FAILED
        assert engine.repos["demo"]["backup"]["last_checked_at"] is None
        assert not engine.git.calls


def test_manual_empty_and_git_failure(factory, monkeypatch):
    with factory(replace(factory.config, sources=())) as engine:
        assert engine.run(manual=True) == Result.NOT_SELECTED
    with factory() as engine:
        monkeypatch.setattr(engine.git, "clone", lambda *a: raise_git_error())
        assert engine.run(manual=True) == Result.BACKUP_FAILED
        assert engine.repos["demo"]["backup"]["last_checked_at"] is None
        assert "last_successful_run_at" not in engine.store.data["scheduler"]


def raise_git_error():
    raise GitError("GIT_FAILED", "unavailable")


@pytest.mark.parametrize("manual_first", [True, False])
def test_manual_cron_overlap_disconnect_and_status(
    factory, monkeypatch, capsys, manual_first
):
    config = replace(factory.config, schedule="* * * * *", manual_run=True)
    monkeypatch.setattr(cli, "load", lambda _: config)
    entered, proceed = Event(), Event()

    def checkpoint(point):
        if point == "journal":
            entered.set()
            assert proceed.wait(10)

    first = datetime.now(UTC)
    with factory(config, checkpoint=checkpoint) as engine:
        scheduler = Scheduler(
            Cron(config.schedule), config.timezone, first, engine.update_skips
        )
        monitor = control.Control(engine, scheduler, engine.git.stop)
        with ThreadPoolExecutor() as pool, monitor.start():
            client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            client.settimeout(3)
            try:
                if manual_first:
                    client.connect(str(control.endpoint(config.root)))
                    client.sendall(b"all")
                else:
                    scheduled = pool.submit(
                        scheduler.tick, first + timedelta(minutes=1), engine.run
                    )
                assert entered.wait(5)
                assert invoke(monkeypatch, "backup", "run", "--all") == Result.BUSY
                assert invoke(monkeypatch, "status", "--json") == 0
                status = json.loads(capsys.readouterr().out)
                assert status["run_status"] == "running"
                assert status["active_run"]["trigger"] == (
                    "manual" if manual_first else "scheduled"
                )
                assert status["last_successful_run_at"] is None
                if manual_first:
                    assert not scheduler.tick(first + timedelta(minutes=1), engine.run)
                    assert scheduler.skipped == 1
                    client.close()
            finally:
                proceed.set()
                client.close()
            if manual_first:
                monitor.worker.join(5)
            else:
                assert scheduled.result(timeout=5)
            assert engine.repos["demo"]["backup"]["successful_updates"] == 1
            assert control.request(config.root, b"status") == Result.SUCCESS


def test_targeted_run_preserves_another_repositories_open_journal(factory):
    config = replace(
        factory.config,
        sources=factory.config.sources
        + (Source("other", "https://example.org/other.git"),),
    )
    with factory(config) as engine:
        operation = engine.store.begin("other", "initial_clone")
        assert engine.run(manual=True, repository="demo") == Result.BACKUP_FAILED
        assert engine.store.data["active_operation"]["id"] == operation
        assert not engine.git.calls


def test_full_manual_run_keeps_independent_source_on_discovery_failure(
    github_env, factory
):
    env = github_env
    config = replace(env.config, sources=factory.config.sources)
    with env.create(config, git_instance=env.git()) as engine:
        env.client.discover.side_effect = DiscoveryError("GITHUB_INVALID_PAGE")
        assert engine.run(manual=True) == Result.DISCOVERY_FAILED
        assert engine.repos["demo"]["backup"]["successful_updates"] == 1
        assert "last_successful_run_at" not in engine.store.data["scheduler"]
        assert not engine.path(env.key).exists()


@pytest.mark.parametrize("fixture", ["github_env", "gitlab_env"])
@pytest.mark.parametrize("target", ["demo", None])
def test_existing_same_host_manual_mirror_updates_during_discovery_failure(
    request, remote, fixture, target
):
    env = request.getfixturevalue(fixture)
    host = env.config.providers[0].host
    config = replace(
        env.config,
        sources=(Source("demo", f"https://{host}/fictional/project.git"),),
    )
    with env.create(config, git_instance=env.git()) as engine:
        assert engine.run() == Result.SUCCESS
        mirror = engine.path("demo") / "repository.git"
        assert (
            engine.repos[env.key]["selection"]["reason"] == "manual_identity_conflict"
        )
        git("update-ref", "refs/heads/new", "HEAD", cwd=remote[1])
        env.client.discover.side_effect = DiscoveryError("INTERRUPTED")

        expected = Result.SUCCESS if target else Result.DISCOVERY_FAILED
        assert engine.run(manual=True, repository=target) == expected
        assert git("rev-parse", "refs/heads/new", cwd=mirror) == git(
            "rev-parse", "HEAD", cwd=remote[1]
        )
        assert not (engine.path(env.key) / "repository.git").exists()


@pytest.mark.parametrize("fixture", ["github_env", "gitlab_env"])
@pytest.mark.parametrize("target", ["demo", None])
@pytest.mark.parametrize("existing_directory", [False, True])
def test_new_same_host_source_waits_for_discovery(
    request, fixture, target, existing_directory
):
    env = request.getfixturevalue(fixture)
    host = env.config.providers[0].host
    config = replace(
        env.config,
        sources=(Source("demo", f"https://{host}/fictional/project.git"),),
    )
    env.client.discover.side_effect = DiscoveryError("INTERRUPTED")
    with env.create(config, git_instance=env.git()) as engine:
        mirror = engine.path("demo") / "repository.git"
        if existing_directory:
            git("init", "--bare", mirror)
        before = sorted(mirror.rglob("*"))
        assert engine.run(manual=True, repository=target) == Result.DISCOVERY_FAILED
        assert not engine.git.calls
        assert mirror.exists() == existing_directory
        assert sorted(mirror.rglob("*")) == before


@pytest.mark.parametrize("fixture", ["github_env", "gitlab_env"])
def test_failed_discovery_keeps_conflicting_and_removed_manual_sources_blocked(
    request, fixture
):
    env = request.getfixturevalue(fixture)
    host = env.config.providers[0].host
    url = f"https://{host}/fictional/project.git"
    with env.create(env.config, git_instance=env.git()) as engine:
        assert engine.run() == Result.SUCCESS
        assert (engine.path(env.key) / "repository.git").is_dir()

    config = replace(env.config, sources=(Source("demo", url),))
    env.client.discover.side_effect = DiscoveryError("INTERRUPTED")
    with env.create(config, git_instance=env.git()) as engine:
        assert engine.run(manual=True, repository="demo") == Result.NOT_SELECTED
        assert engine.run(manual=True) == Result.DISCOVERY_FAILED
        assert not engine.git.calls

    with env.create(env.config, git_instance=env.git()) as engine:
        assert engine.repos["demo"]["selection"]["status"] == "excluded"
        assert engine.run(manual=True, repository="demo") == Result.NOT_SELECTED
        assert (
            engine.run(manual=True, repository="unknown") == Result.UNKNOWN_REPOSITORY
        )
        assert not engine.git.calls
