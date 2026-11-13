"""A capacity pause is visible separately from an exhausted repair budget."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import LocalGit, git

from repoharbor.persistence import read_json
from repoharbor.recovery import recover
from repoharbor.storage import usage


def damage(factory):
    with factory() as engine:
        engine.run()
        folder = engine.path("demo")
        mirror = folder / "repository.git"
        refs = engine.git.refs(mirror)
        oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
        (mirror / "objects" / oid[:2] / oid[2:]).unlink()
        archive = folder / "quarantine/initial-clone"
        archive.mkdir(parents=True)
        (archive / "precious").write_text("preserved archive")
        engine.repos["demo"]["integrity"]["status"] = "suspect"
        engine.save_meta("demo")
        return folder, refs, engine.repos["demo"]["backup"].copy()


@pytest.mark.parametrize("kind", ["global", "repo", "reserve"])
def test_repair_pause_survives_restart_and_resumes_next_run(
    factory, tmp_path, monkeypatch, kind
):
    folder, refs, before = damage(factory)
    storage = factory.config.storage
    burden = folder / "quarantine/initial-clone/burden"
    free = [500]
    if kind == "global":
        storage = replace(storage, global_bytes=usage(factory.root) + 16384)
        expected = "global_limit_paused"
    elif kind == "repo":
        storage = replace(storage, repo_bytes=usage(folder) + 16384)
        expected = "repo_limit_reached"
    else:
        storage = replace(storage, min_free_bytes=1000)
        expected = "FREE_SPACE_RESERVE"
        monkeypatch.setattr(
            "repoharbor.storage.shutil.disk_usage",
            lambda _: SimpleNamespace(free=free[0]),
        )
    if kind != "reserve":
        burden.write_bytes(b"x" * 65536)
    config = replace(factory.config, storage=storage)
    with factory(config) as engine:
        recover(engine)
        meta = engine.repos["demo"]
        assert meta["backup"]["status"] == "repair_paused"
        assert meta["backup"]["repair_deferred"] == expected
        assert meta["integrity"]["repair_attempts"] == 0
        assert meta["errors"]["total"] == 0
        assert not any("fetch" in args for args in engine.git.calls)
        assert meta["backup"]["last_checked_at"] == before["last_checked_at"]
        assert meta["backup"]["last_updated_at"] == before["last_updated_at"]
        journal_id = engine.store.data["active_operation"]["id"]
    with factory(config) as engine:
        recover(engine)
        meta = engine.repos["demo"]
        assert (
            read_json(folder / "metadaten.json")["backup"]["status"] == "repair_paused"
        )
        assert engine.store.data["active_operation"]["id"] == journal_id
        assert engine.git.refs(folder / "repository.git") == refs
        assert (
            folder / "quarantine/initial-clone/precious"
        ).read_text() == "preserved archive"
        # Simulate explicit operator archival, or free space returning on the volume.
        if burden.exists():
            burden.rename(tmp_path / "operator-preserved-burden")
        free[0] = 10000
        assert not any("fetch" in args for args in engine.git.calls)
        engine.run()  # The next regular cron run, without another service restart.
        assert engine.git.healthy(folder / "repository.git")
        assert meta["backup"]["status"] == "up_to_date"
        assert "repair_deferred" not in meta["backup"]
        assert meta["integrity"]["successful_repairs"] == 1
        assert meta["integrity"]["repair_attempts"] == 0
        assert meta["backup"]["last_updated_at"] == before["last_updated_at"]
        assert engine.git.refs(folder / "repository.git") == refs
        assert (folder / "quarantine/initial-clone/precious").exists()


def test_exhausted_repair_stays_failed_even_when_capacity_is_blocked(
    factory, remote, tmp_path
):
    folder, refs, _ = damage(factory)
    remote[1].rename(tmp_path / "offline-source.git")
    with factory() as engine:
        recover(engine)  # Three real failing fetches from an unavailable local remote.
        assert engine.repos["demo"]["backup"]["status"] == "repair_failed"
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 3
        assert engine.repos["demo"]["errors"]["total"] == 3
    blocked = replace(
        factory.config, storage=replace(factory.config.storage, global_bytes=0)
    )
    with factory(blocked) as engine:
        recover(engine)
        engine.repos["demo"]["integrity"]["status"] = "suspect"
        recover(engine)
        meta = engine.repos["demo"]
        assert meta["backup"]["status"] == "repair_failed"
        assert "repair_deferred" not in meta["backup"]
        assert meta["integrity"]["repair_attempts"] == 3
        assert meta["errors"]["total"] == 3
        assert not any("fetch" in args for args in engine.git.calls)
        assert engine.git.refs(folder / "repository.git") == refs
        assert (folder / "quarantine/initial-clone/precious").exists()


@pytest.mark.parametrize("pause", ["before_fetch", "during_fetch"])
def test_paused_first_repair_does_not_starve_second_repository(
    factory, remote, tmp_path, monkeypatch, pause
):
    from repoharbor.config import Source
    from repoharbor.errors import HarborError

    class TwoRemotes(LocalGit):
        def run(self, args, credential=None, interrupt=lambda: False):
            args = [
                "https://example.org/repo.git"
                if arg == "https://example.org/other.git"
                else arg
                for arg in args
            ]
            return super().run(args, credential, interrupt)

    config = replace(
        factory.config,
        sources=(
            *factory.config.sources,
            Source("other", "https://example.org/other.git"),
        ),
    )
    with factory(config, git_instance=TwoRemotes(remote[1])) as engine:
        # Both synthetic sources use the same local remote, with independent mirrors.
        source = config.sources[0]
        engine.backup(source)
        engine.git.clone(
            source.url, engine.path("other") / "repository.git", None, lambda: False
        )
        refs = {}
        for alias in engine.repos:
            mirror = engine.path(alias) / "repository.git"
            refs[alias] = engine.git.refs(mirror)
            oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
            (mirror / "objects" / oid[:2] / oid[2:]).unlink()
            engine.repos[alias]["integrity"]["status"] = "suspect"
            engine.save_meta(alias)
        operation = engine.store.begin("demo", "repair")
        engine.store.update_operation(state="interrupted")
        folder = engine.path("demo")
        burden = folder / "operator-archive"
        limit = usage(folder) + 16384
        if pause == "before_fetch":
            burden.write_bytes(b"x" * 65536)
    config = replace(config, storage=replace(config.storage, overrides={"demo": limit}))
    with factory(config, git_instance=TwoRemotes(remote[1])) as engine:
        original = engine.git.fetch

        def fetch(mirror, *args, **kwargs):
            if mirror.parent.name == "demo":
                burden.write_bytes(b"x" * 65536)
                raise HarborError("repo_limit_reached")
            return original(mirror, *args, **kwargs)

        monkeypatch.setattr(engine.git, "fetch", fetch)
        recover(engine)
        assert engine.repos["other"]["integrity"]["status"] == "healthy"
        assert engine.repos["other"]["integrity"]["successful_repairs"] == 1
        assert engine.repos["demo"]["integrity"]["repair_attempts"] == 0
        assert engine.repos["demo"]["errors"]["total"] == 0
        assert engine.store.data["active_operation"]["id"] != operation
        for _ in range(2):
            recover(engine)
            engine.run()
        assert engine.repos["other"]["integrity"]["successful_repairs"] == 1
    with factory(config, git_instance=TwoRemotes(remote[1])) as engine:
        for _ in range(2):
            recover(engine)
            engine.run()
        assert engine.repos["demo"]["backup"]["status"] == "repair_paused"
        assert engine.repos["demo"]["backup"]["repair_deferred"] == "repo_limit_reached"
        assert engine.repos["other"]["integrity"]["successful_repairs"] == 1
        assert burden.read_bytes() == b"x" * 65536
        burden.rename(tmp_path / "operator-preserved-archive")
        engine.run()
        for _ in range(2):
            recover(engine)
        for alias in engine.repos:
            mirror = engine.path(alias) / "repository.git"
            assert engine.git.healthy(mirror)
            assert engine.git.refs(mirror) == refs[alias]
            assert engine.repos[alias]["integrity"]["successful_repairs"] == 1
            assert engine.repos[alias]["integrity"]["repair_attempts"] == 0
            assert engine.repos[alias]["errors"]["total"] == 0
        assert "repair_deferred" not in engine.repos["demo"]["backup"]
        assert (tmp_path / "operator-preserved-archive").exists()
