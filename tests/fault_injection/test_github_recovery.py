import copy
import errno
from dataclasses import replace

import pytest
from conftest import raw_repo
from test_crash_corrections import kill_at

from repoharbor.errors import HarborError
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
