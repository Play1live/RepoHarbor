import copy
import errno
import io
from dataclasses import replace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
from conftest import api_response, raw_repo
from test_crash_corrections import kill_at

from repoharbor.config import Discovery, Source
from repoharbor.errors import HarborError
from repoharbor.github import GitHub
from repoharbor.persistence import read_json
from repoharbor.recovery import recover
from repoharbor.selection import Selection


@pytest.mark.parametrize(
    "point", ["discovery_metadata", "journal", "git", "promoted", "metadata"]
)
def test_provider_sigkill_replay_never_clones_on_startup_or_counts_twice(
    github_env, point
):
    env = github_env

    def create(**kwargs):
        return env.create(env.config, git_instance=env.git(), **kwargs)

    kill_at(create, lambda engine: engine.run(), point)
    with create() as engine:
        recover(engine)
        assert not any("fetch" in c or "clone" in c for c in engine.git.calls)
        assert engine.sources == ()
        if point in ("git", "promoted", "metadata"):
            assert engine.repos[env.key]["backup"]["successful_updates"] == 1
        else:
            assert engine.repos[env.key]["backup"]["successful_updates"] == 0
        engine.run()
        assert engine.repos[env.key]["backup"]["successful_updates"] == 1
        expected = (
            "reconciled" if point in ("git", "promoted", "metadata") else "completed"
        )
        assert engine.store.data["active_operation"]["state"] == expected
        assert len(list(env.config.root.rglob("repository.git"))) == 1


def test_crash_during_selection_publication_requires_fresh_discovery(github_env):
    env = github_env
    env.results["first"].append(raw_repo(18, name="another"))
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        before = {k: copy.deepcopy(v["backup"]) for k, v in engine.repos.items()}
    cfg = replace(env.config, selection=Selection(mode="selected"))

    def create(**kwargs):
        return env.create(cfg, git_instance=env.git(), **kwargs)

    kill_at(create, lambda engine: engine.run(), "discovery_metadata")
    with create() as engine:
        assert engine.store.data["discovery"]["state"] == "running"
        recover(engine)
        assert not engine.git.calls
        engine.run()
        assert engine.sources == ()
        for key, meta in engine.repos.items():
            assert meta["selection"]["status"] == "excluded"
            assert meta["backup"] == before[key]
            assert (engine.path(key) / "repository.git").exists()


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO])
def test_orphan_metadata_directory_after_io_failure_fails_closed(
    github_env, monkeypatch, code
):
    env = github_env
    with env.create(env.config, git_instance=env.git()) as engine:

        def fail(alias):
            raise OSError(code, "injected")

        monkeypatch.setattr(engine, "save_meta", fail)
        with pytest.raises(OSError):
            engine.run()
        assert engine.sources == ()
        journal = read_json(env.config.root / "data.json")
        assert journal["discovery"]["state"] == "running"
    before = (env.config.root / "data.json").read_bytes()
    with pytest.raises(HarborError), env.create(env.config, git_instance=env.git()):
        pass
    assert (env.config.root / "data.json").read_bytes() == before
    assert not list(env.config.root.rglob("repository.git"))


def test_sigkill_after_rate_limit_keeps_long_cooldown_and_existing_mirrors(
    github_env, monkeypatch, github_clock
):
    env = github_env
    cfg = replace(
        env.config,
        sources=(Source("demo", "https://example.org/repo.git"),),
        discovery=Discovery(0),
    )
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        meta_path = engine.path(env.key) / "metadaten.json"
        before = meta_path.read_bytes()
        mirror = engine.path(env.key) / "repository.git"
        inode = mirror.stat().st_ino
    until = github_clock.time() + 172800
    monkeypatch.setattr("time.time", github_clock.time)
    monkeypatch.setattr("repoharbor.engine.GitHub", GitHub)
    opener = Mock()
    opener.open.side_effect = HTTPError(
        "", 429, "", {"retry-after": "172800"}, io.BytesIO()
    )
    monkeypatch.setattr("repoharbor.github.build_opener", lambda *args: opener)

    def create(**kwargs):
        return env.create(cfg, git_instance=env.git(), **kwargs)

    def discover_and_pause(engine):
        engine.discover()
        assert engine.store.data["discovery"]["retry_at"] == until
        engine.checkpoint("cooldown_persisted")

    kill_at(create, discover_and_pause, "cooldown_persisted")
    assert read_json(cfg.root / "data.json")["discovery"]["retry_at"] == until
    github_clock.elapsed = 90000
    with create() as engine:
        recover(engine)
        engine.run()
        opener.open.assert_not_called()
        assert engine.store.data["discovery"]["retry_at"] == until
        assert engine.sources == cfg.sources
        assert meta_path.read_bytes() == before
        assert mirror.stat().st_ino == inode and engine.git.healthy(mirror)
        assert engine.repos["demo"]["backup"]["status"] == "up_to_date"


def test_sigkill_during_publication_keeps_http_200_cooldown(
    github_env, monkeypatch, github_clock
):
    env = github_env
    cfg = replace(
        env.config, discovery=Discovery(0), providers=env.config.providers[:1]
    )
    until = github_clock.time() + 172800
    monkeypatch.setattr("repoharbor.engine.GitHub", GitHub)
    monkeypatch.setattr("time.time", github_clock.time)
    opener = Mock()
    opener.open.side_effect = [
        api_response({"login": "fictional"}),
        api_response(
            [raw_repo()],
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(until)},
        ),
    ]
    monkeypatch.setattr("repoharbor.github.build_opener", lambda *args: opener)

    def create(**kwargs):
        return env.create(cfg, git_instance=env.git(), **kwargs)

    kill_at(create, lambda engine: engine.run(), "discovery_metadata")
    state = read_json(cfg.root / "data.json")["discovery"]
    assert state["state"] == "running" and state["retry_at"] == until
    github_clock.elapsed = 90000
    with create() as engine:
        recover(engine)
        engine.run()
        opener.open.assert_not_called()
        assert engine.sources == ()
        assert engine.store.data["discovery"]["retry_at"] == until
        assert not engine.git.calls
