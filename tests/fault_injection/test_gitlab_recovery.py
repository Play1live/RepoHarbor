import copy
import errno
from dataclasses import replace

import pytest
from test_crash_corrections import kill_at

from repoharbor.config import Discovery
from repoharbor.errors import HarborError
from repoharbor.persistence import read_json
from repoharbor.recovery import recover
from repoharbor.selection import Selection


@pytest.mark.parametrize(
    "point", ["discovery_metadata", "journal", "git", "promoted", "metadata"]
)
def test_gitlab_sigkill_recovery_requires_fresh_discovery(gitlab_env, point):
    env = gitlab_env

    def create(**kwargs):
        return env.create(env.config, git_instance=env.git(), **kwargs)

    kill_at(create, lambda engine: engine.run(), point)
    with create() as engine:
        recover(engine)
        assert not any("fetch" in c or "clone" in c for c in engine.git.calls)
        assert engine.sources == ()
        engine.run()
        assert engine.repos[env.key]["backup"]["successful_updates"] == 1
        assert len(list(env.config.root.rglob("repository.git"))) == 1


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO])
def test_gitlab_metadata_io_failure_preserves_state(gitlab_env, monkeypatch, code):
    env = gitlab_env
    with env.create(env.config, git_instance=env.git()) as engine:

        def fail(alias):
            raise OSError(code, "injected")

        monkeypatch.setattr(engine, "save_meta", fail)
        with pytest.raises(OSError):
            engine.run()
        assert engine.sources == ()
        assert not list(env.config.root.rglob("repository.git"))
    with pytest.raises(HarborError), env.create(env.config, git_instance=env.git()):
        pass


def test_gitlab_partial_selection_crash_preserves_mirror(gitlab_env):
    env = gitlab_env
    with env.create(env.config, git_instance=env.git()) as engine:
        engine.run()
        before = copy.deepcopy(engine.repos[env.key]["backup"])
    cfg = replace(env.config, selection=Selection(mode="selected"))

    def create(**kwargs):
        return env.create(cfg, git_instance=env.git(), **kwargs)

    kill_at(create, lambda e: e.run(), "discovery_metadata")
    with create() as engine:
        recover(engine)
        assert not engine.git.calls
        engine.run()
        assert not engine.sources
        assert engine.repos[env.key]["backup"] == before


def test_cooldown_survives_crash_while_success_body_is_read(gitlab_env, monkeypatch):
    import time
    from unittest.mock import Mock

    from conftest import api_response

    from repoharbor.gitlab import GitLab

    env = gitlab_env
    cfg = replace(
        env.config, providers=env.config.providers[:1], discovery=Discovery(0)
    )
    monkeypatch.setattr("repoharbor.engine.GitLab", GitLab)
    opener = Mock()
    until = time.time() + 172800
    monkeypatch.setattr("repoharbor.github.build_opener", lambda *a: opener)

    def create(**kwargs):
        return env.create(cfg, git_instance=env.git(), **kwargs)

    def action(engine):
        response = api_response(
            {"id": 1, "username": "fictional"},
            {
                "RateLimit-Remaining": "0",
                "RateLimit-Reset": str(until),
            },
        )
        read = response.read

        def checkpoint_read(*args):
            engine.checkpoint("cooldown_body")
            return read(*args)

        response.read = checkpoint_read
        opener.open.return_value = response
        engine.run()

    kill_at(create, action, "cooldown_body")
    key = "gitlab:gitlab.example.org"
    assert (
        read_json(cfg.root / "data.json")["provider_discovery"][key]["retry_at"]
        == until
    )
    with create() as engine:
        recover(engine)
        engine.run()
        opener.open.assert_not_called()
        assert not engine.git.calls
        assert not engine.sources
