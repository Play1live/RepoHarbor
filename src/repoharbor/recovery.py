"""Local reconciliation only at boot; never backfill a scheduled backup."""

import os

from repoharbor.engine import Engine
from repoharbor.errors import HarborError
from repoharbor.persistence import record_error, sync_directory, utcnow


def recover(engine: Engine) -> None:
    store = engine.store
    active = store.data["active_operation"]
    if active is not None and (
        not isinstance(active, dict)
        or not {"alias", "type", "id", "state"} <= active.keys()
    ):
        raise HarborError("Invalid operation journal")
    if active and active["alias"] not in engine.repos:
        raise HarborError("Journal references unknown repository")
    for alias, meta in engine.repos.items():
        folder = engine.path(alias)
        partial, mirror = folder / "repository.git.partial", folder / "repository.git"
        interrupted = bool(
            active
            and active["alias"] == alias
            and active["state"] in ("running", "interrupted")
        )
        if not (
            interrupted or partial.exists() or meta["integrity"]["status"] == "suspect"
        ):
            continue
        backup, integrity = meta["backup"], meta["integrity"]
        # A healthy partial cannot prove clone completion after SIGKILL.
        # Preserve it for explicit inspection; do not promote or silently delete it.
        if partial.exists():
            if (
                interrupted
                and active is not None
                and active.get("git_completed_at")
                and active["type"] == "initial_clone"
                and not mirror.exists()
                and engine.git.healthy(partial)
            ):
                os.rename(partial, mirror)
                sync_directory(folder)
            else:
                backup["status"] = "interrupted"
                integrity["status"] = "suspect"
        if mirror.exists():
            healthy = engine.git.healthy(mirror)
            integrity["last_checked_at"] = utcnow()
            integrity["status"] = "healthy" if healthy else "corrupt"
            if healthy:
                integrity["repair_attempts"] = 0
                if (
                    interrupted
                    and active is not None
                    and backup["completed_operation"] != active["id"]
                ):
                    # Git may have succeeded; completion time/refs cannot be proven.
                    if active.get("git_completed_at"):
                        engine.finish(alias, active)
                    else:
                        backup["status"] = "interrupted"
            else:
                repair(engine, alias)
        else:
            backup["status"] = "interrupted"
        engine.save_meta(alias)
    if active and active["state"] in ("running", "interrupted"):
        active["state"] = "reconciled"
        active["completed_at"] = utcnow()
    if store.data["active_run"] and store.data["active_run"]["state"] == "running":
        store.data["active_run"]["state"] = "interrupted"
    store.data["recovery"]["last_checked_at"] = utcnow()
    store.save()


def repair(engine: Engine, alias: str) -> None:
    meta = engine.repos[alias]
    integrity, backup = meta["integrity"], meta["backup"]
    source = next((s for s in engine.config.sources if s.alias == alias), None)
    if (
        source is None
        or not engine.config.automatic_repair
        or engine.budget.status(alias, engine.path(alias))
    ):
        backup["status"] = "repair_failed"
        return
    mirror = engine.path(alias) / "repository.git"
    while integrity["repair_attempts"] < engine.config.repair_attempts:
        integrity["repair_attempts"] += 1
        integrity["status"] = backup["status"] = "repairing"
        engine.save_meta(alias)
        engine.store.begin(alias, "repair")
        try:
            engine.git.fetch(
                mirror,
                source.url,
                source.credential,
                lambda: engine.budget.interrupt(alias, engine.path(alias)),
                repair=True,
            )
            healthy = engine.git.healthy(mirror)
        except HarborError:
            healthy = False
        integrity["last_repair_at"] = utcnow()
        integrity["last_repair_result"] = "healthy" if healthy else "failed"
        if healthy:
            integrity["status"] = "healthy"
            integrity["successful_repairs"] += 1
            integrity["repair_attempts"] = 0
            backup["status"] = "interrupted"
        else:
            record_error(meta, "REPAIR_FAILED")
            integrity["status"] = backup["status"] = "repair_failed"
        engine.save_meta(alias)
        engine.store.complete()
        if healthy:
            return
    integrity["status"] = backup["status"] = "repair_failed"
