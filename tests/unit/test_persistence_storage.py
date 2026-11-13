import errno
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from repoharbor.config import Storage
from repoharbor.errors import HarborError
from repoharbor.persistence import (
    Store,
    atomic_json,
    metadata,
    read_json,
    record_error,
    safe_tree,
)
from repoharbor.storage import Budget, usage


def test_live_measurement_tolerates_removed_file(tmp_path, monkeypatch):
    victim = tmp_path / "temporary.pack"
    victim.write_bytes(b"temporary")
    (tmp_path / "retained.pack").write_bytes(b"retained")
    is_file = Path.is_file

    def remove_after_check(path):
        result = is_file(path)
        if path == victim:
            path.unlink()
        return result

    monkeypatch.setattr(Path, "is_file", remove_after_check)
    budget = Budget(tmp_path, Storage(min_free_bytes=0, during_transfer="abort"))
    assert budget.monitor("demo", tmp_path)() is False
    assert usage(tmp_path) == len(b"retained")


@pytest.mark.parametrize("code", [errno.ENOENT, errno.EACCES, errno.EIO])
@pytest.mark.parametrize("phase", ["stat", "walk"])
def test_measurement_only_ignores_disappearance(tmp_path, monkeypatch, code, phase):
    victim = tmp_path / "temporary.lock"
    victim.touch()
    error = OSError(code, "synthetic")
    if phase == "stat":
        stat = Path.stat

        def failing_stat(path, **kwargs):
            if path == victim and kwargs.get("follow_symlinks", True):
                raise error
            return stat(path, **kwargs)

        monkeypatch.setattr(Path, "stat", failing_stat)
    else:

        def failing_walk(path, onerror):
            onerror(error)
            return iter(())

        monkeypatch.setattr("repoharbor.storage.os.walk", failing_walk)
    if code == errno.ENOENT:
        assert usage(tmp_path) == 0
    else:
        with pytest.raises(OSError) as caught:
            usage(tmp_path)
        assert caught.value.errno == code


def test_atomic_roundtrip_error_ring_and_lock(tmp_path):
    meta = metadata("demo", "https://example.org/r", None, None)
    for _ in range(25):
        record_error(meta, "EIO")
    assert meta["errors"]["total"] == 25 and len(meta["errors"]["recent"]) == 20
    path = tmp_path / "meta.json"
    atomic_json(path, meta)
    assert read_json(path) == meta
    path.unlink()
    with Store(tmp_path) as store:
        with pytest.raises(BlockingIOError), Store(tmp_path):
            pass
        op = store.begin("demo", "initial_clone")
        assert store.data["active_operation"]["id"] == op
        store.complete()
    with Store(tmp_path) as reopened:
        assert reopened.data["active_operation"]["state"] == "completed"


@pytest.mark.parametrize(
    "text", ["{bad", "[]", '{"schema_version":2}', '{"schema_version":true}', "\udcff"]
)
def test_corrupt_json_preserved(tmp_path, text):
    path = tmp_path / "data.json"
    path.write_text(text, errors="surrogateescape")
    before = path.read_bytes()
    with pytest.raises(HarborError), Store(tmp_path):
        pass
    assert path.read_bytes() == before


def test_missing_incomplete_and_unsafe_state(tmp_path):
    (tmp_path / "unknown").mkdir()
    with pytest.raises(HarborError), Store(tmp_path):
        pass
    assert not (tmp_path / "data.json").exists()
    (tmp_path / "unknown").rmdir()
    atomic_json(tmp_path / "data.json", {"schema_version": 1})
    with pytest.raises(HarborError), Store(tmp_path):
        pass
    (tmp_path / "link").symlink_to(tmp_path / "data.json")
    with pytest.raises(HarborError):
        safe_tree(tmp_path)
    with pytest.raises(HarborError):
        read_json(tmp_path / "link")
    with pytest.raises(HarborError):
        atomic_json(tmp_path / "link", {})
    with pytest.raises(HarborError):
        usage(tmp_path)
    with pytest.raises(HarborError):
        safe_tree(tmp_path / "link")
    (tmp_path / "link").unlink()
    (tmp_path / "parent-link").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "child").mkdir()
    with pytest.raises(HarborError):
        safe_tree(tmp_path / "parent-link" / "child")


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC, errno.EACCES])
@pytest.mark.parametrize("phase", ["fsync", "replace"])
def test_atomic_failure_keeps_old_document(tmp_path, code, phase):
    path = tmp_path / "data.json"
    atomic_json(path, {"schema_version": 1, "old": True})
    with (
        patch(
            f"repoharbor.persistence.os.{phase}", side_effect=OSError(code, "synthetic")
        ),
        pytest.raises(OSError),
    ):
        atomic_json(path, {"schema_version": 1, "old": False})
    assert read_json(path)["old"] is True
    assert list(tmp_path.glob("*.tmp")) == []


def test_budgets_count_excluded_partial_overrides_and_reserve(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "repository.git.partial").write_bytes(b"x" * 20)
    (tmp_path / "excluded").write_bytes(b"x" * 30)
    assert usage(tmp_path) == 50
    cfg = Storage(global_bytes=50, min_free_bytes=0)
    budget = Budget(tmp_path, cfg)
    assert budget.status("demo", repo) == "global_limit_paused"
    assert not budget.interrupt("demo", repo)
    budget.config = replace(cfg, during_transfer="abort")
    assert budget.interrupt("demo", repo)
    budget.config = replace(cfg, global_bytes=None, repo_bytes=20)
    assert budget.status("demo", repo) == "repo_limit_reached"
    budget.config = replace(budget.config, overrides={"demo": 21})
    assert budget.status("demo", repo) is None
    budget.config = replace(cfg, global_bytes=0)
    assert budget.status("demo", repo) == "global_limit_paused"
    budget.config = replace(cfg, min_free_bytes=10**30)
    assert budget.status("demo", repo) == "FREE_SPACE_RESERVE"
    assert budget.interrupt("demo", repo)
    (tmp_path / "excluded").unlink()
    budget.config = cfg
    assert budget.status("demo", repo) is None


@pytest.mark.parametrize(
    "section,value",
    [
        ("repository", {}),
        ("backup", None),
        ("errors", {"total": -1}),
        ("schema_version", 2),
    ],
)
def test_broken_metadata_preserved(factory, section, value):
    with factory() as engine:
        path = engine.path("demo") / "metadaten.json"
    data = json.loads(path.read_text())
    data[section] = value
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(HarborError), factory():
        pass
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(scheduler=[]),
        lambda d: d["scheduler"].update(skipped_overlaps=-1),
        lambda d: d.update(global_storage=[]),
        lambda d: d.update(recovery={}),
        lambda d: d.update(active_run={"state": "unknown"}),
        lambda d: d.update(active_run={"state": "running", "started_at": 4}),
        lambda d: d.update(
            active_operation={"state": "running", "started_at": "now", "alias": 4}
        ),
        lambda d: d.update(
            active_operation={
                "state": "running",
                "started_at": "now",
                "alias": "demo",
                "id": "x",
                "type": "initial_clone",
                "git_completed_at": "now",
            }
        ),
        lambda d: d.update(active_operation={"state": [], "started_at": "now"}),
    ],
)
def test_semantically_corrupt_journal_is_not_changed(tmp_path, change):
    with Store(tmp_path) as store:
        data = store.data
    change(data)
    atomic_json(tmp_path / "data.json", data)
    before = (tmp_path / "data.json").read_bytes()
    with pytest.raises(HarborError), Store(tmp_path):
        pass
    assert (tmp_path / "data.json").read_bytes() == before
