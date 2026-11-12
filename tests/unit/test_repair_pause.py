"""A capacity pause is visible separately from an exhausted repair budget."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import git

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
