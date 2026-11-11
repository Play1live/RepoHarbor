import subprocess
import threading
from contextlib import contextmanager

import pytest

from repoharbor.config import Config, Source, Storage
from repoharbor.credentials import Credentials
from repoharbor.engine import Engine
from repoharbor.git_backup import Git
from repoharbor.persistence import Store


def git(*args, cwd=None):
    return subprocess.check_output(["git", *map(str, args)], cwd=cwd, text=True).strip()


@pytest.fixture
def remote(tmp_path):
    work = tmp_path / "work"
    git("init", "-b", "main", work)
    git("config", "user.name", "Example", cwd=work)
    git("config", "user.email", "test@example.org", cwd=work)
    (work / "hello.txt").write_text("first\n")
    git("add", ".", cwd=work)
    git("commit", "-m", "initial", cwd=work)
    bare = tmp_path / "remote.git"
    git("clone", "--bare", work, bare)
    git("remote", "add", "origin", bare, cwd=work)
    return work, bare


class LocalCredentials(Credentials):
    """Only test harnesses can enable local transport; no product config flag."""

    def git_env(self, name):
        env = super().git_env(name)
        count = int(env["GIT_CONFIG_COUNT"])
        env.update(
            {
                "GIT_CONFIG_COUNT": str(count + 1),
                f"GIT_CONFIG_KEY_{count}": "protocol.file.allow",
                f"GIT_CONFIG_VALUE_{count}": "always",
            }
        )
        return env


class LocalGit(Git):
    def __init__(self, bare):
        super().__init__(LocalCredentials({}), threading.Event())
        self.bare = bare
        self.calls = []

    def run(self, args, credential=None, interrupt=lambda: False):
        self.calls.append(args)
        args = [
            str(self.bare) if a == "https://example.org/repo.git" else a for a in args
        ]
        return super().run(args, credential, interrupt)


@pytest.fixture
def factory(tmp_path, remote):
    root = tmp_path / "backups"
    root.mkdir()
    cfg = Config(
        (Source("demo", "https://example.org/repo.git"),),
        (),
        "env",
        root=root,
        storage=Storage(min_free_bytes=0),
    )

    @contextmanager
    def create(config=None, checkpoint=None, git_instance=None):
        config = config or cfg
        with Store(root) as store:
            engine = Engine(config, store, git_instance or LocalGit(remote[1]))
            if checkpoint is not None:
                engine.checkpoint = checkpoint
            engine.inventory()
            yield engine

    create.config = cfg
    create.root = root
    return create
