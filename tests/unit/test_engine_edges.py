from dataclasses import replace

import pytest

from repoharbor.config import Storage
from repoharbor.errors import HarborError
from repoharbor.persistence import atomic_json, read_json
from repoharbor.recovery import recover


def test_stop_limits_partial_and_suspect(factory, monkeypatch):
    with factory() as engine:
        engine.git.stop.set()
        engine.run()
        assert engine.store.data["active_run"]["state"] == "interrupted"
        engine.git.stop.clear()
        engine.config = replace(
            engine.config, storage=Storage(global_bytes=0, min_free_bytes=0)
        )
        engine.budget.config = engine.config.storage
        engine.run()
        assert engine.repos["demo"]["backup"]["status"] == "global_limit_paused"
        assert engine.repos["demo"]["backup"]["last_checked_at"] is None
        engine.budget.config = Storage(min_free_bytes=0)
        partial = engine.path("demo") / "repository.git.partial"
        partial.mkdir()
        engine.run()
        assert engine.repos["demo"]["backup"]["status"] == "interrupted"
        partial.rmdir()
        engine.run()
        engine.repos["demo"]["integrity"]["status"] = "corrupt"
        calls = len(engine.git.calls)
        engine.run()
        assert len(engine.git.calls) == calls
        engine.repos["demo"]["integrity"]["status"] = "healthy"

        def interrupted(*a, **kw):
            engine.git.stop.set()
            raise HarborError("INTERRUPTED")

        monkeypatch.setattr(engine.git, "remote_refs", interrupted)
        engine.run()
        assert engine.repos["demo"]["backup"]["status"] == "interrupted"


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m["repository"].update(id="other"),
        lambda m: m["errors"].update(recent=[{}] * 21),
        lambda m: m["errors"].update(total=-1),
        lambda m: m["backup"].update(successful_updates="bad"),
    ],
)
def test_metadata_semantics(factory, change):
    with factory() as engine:
        path = engine.path("demo") / "metadaten.json"
    meta = read_json(path)
    change(meta)
    atomic_json(path, meta)
    with pytest.raises(HarborError), factory():
        pass


def test_suspect_healthy_mirror_without_active_operation(factory):
    with factory() as engine:
        engine.run()
        engine.store.data["active_operation"] = None
        engine.repos["demo"]["integrity"]["status"] = "suspect"
        recover(engine)
        assert engine.repos["demo"]["integrity"]["status"] == "healthy"
        engine.store.__exit__()
        engine.store.__exit__()  # closing twice is harmless
