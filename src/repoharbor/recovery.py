"""Crash reconciliation and bounded, non-destructive recovery (never backfill)."""

import logging
import os
from typing import Any

from repoharbor.engine import Engine
from repoharbor.errors import GitError, HarborError
from repoharbor.persistence import record_error, sync_directory, utcnow


def attention(engine: Engine, alias: str, reason: str) -> None:
    backup = engine.repos[alias]["backup"]
    backup["status"] = "interrupted"
    if backup.get("manual_attention_required") != reason:
        logging.warning("manual attention required: %s (%s)", alias, reason)
    backup["manual_attention_required"] = reason
    engine.repos[alias]["integrity"]["status"] = "suspect"
    engine.save_meta(alias)


def quarantine(engine: Engine, alias: str, active: dict[str, Any] | None) -> None:
    """One preserved slot per repository, journaled before rename, safe to replay."""
    folder, meta = engine.path(alias), engine.repos[alias]
    partial = folder / "repository.git.partial"
    destination = folder / "quarantine" / "initial-clone"
    backup = meta["backup"]
    pending = backup.get("quarantine", {})
    owned = bool(
        active
        and active["alias"] == alias
        and active["type"] == "initial_clone"
        and active["state"] in ("running", "interrupted", "reconciled")
    )
    owner = (
        active["id"]
        if owned and active is not None
        else backup.get("partial_operation")
    )
    owned = owned or isinstance(owner, str)
    if meta["selection"]["status"] == "excluded":
        attention(engine, alias, "EXCLUDED_PARTIAL_PRESERVED")
        return
    if destination.parent.exists() and not destination.parent.is_dir():
        attention(engine, alias, "QUARANTINE_CONFLICT")
        return
    if pending.get("state") != "pending":
        if not owned or destination.exists() or (folder / "repository.git").exists():
            attention(engine, alias, "PARTIAL_UNOWNED_OR_QUARANTINE_FULL")
            return
        backup["quarantine"] = {
            "state": "pending",
            "operation_id": owner,
            "relative_path": "quarantine/initial-clone",
        }
        engine.save_meta(alias)
    current = engine.store.data["active_operation"]
    if not (current and current["alias"] == alias and current["type"] == "quarantine"):
        engine.store.begin(alias, "quarantine")
    engine.store.update_operation(quarantine_pending=True)
    engine.checkpoint("quarantine_journal")
    if partial.exists() and destination.exists():
        attention(engine, alias, "QUARANTINE_CONFLICT")
        return
    if partial.exists():
        destination.parent.mkdir(exist_ok=True)
        sync_directory(folder)
        os.rename(partial, destination)
        sync_directory(destination.parent)
        sync_directory(folder)
        engine.checkpoint("quarantine_renamed")
    if not destination.exists():
        attention(engine, alias, "QUARANTINE_CONTENT_MISSING")
        return
    backup["quarantine"]["state"] = "preserved"
    backup.pop("manual_attention_required", None)
    backup["status"] = "interrupted"
    meta["integrity"]["status"] = "unchecked"
    engine.save_meta(alias)
    engine.checkpoint("quarantine_metadata")


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
    # Resolve the existing active operation before another source can journal a move.
    aliases = sorted(
        engine.repos,
        key=lambda alias: (not (active and active["alias"] == alias), alias),
    )
    for alias in aliases:
        meta = engine.repos[alias]
        if engine.git.stop.is_set():
            break
        folder = engine.path(alias)
        partial, mirror = folder / "repository.git.partial", folder / "repository.git"
        interrupted = bool(
            active
            and active["alias"] == alias
            and active["state"] in ("running", "interrupted")
        )
        backup, integrity = meta["backup"], meta["integrity"]
        pending_quarantine = backup.get("quarantine", {}).get("state") == "pending"
        if not (
            interrupted
            or partial.exists()
            or pending_quarantine
            or integrity["status"] in ("suspect", "repairing")
            or backup["status"] == "repairing"
            or "manual_attention_required" in backup
            or backup.get("repair_deferred")
        ):
            continue
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
                quarantine(engine, alias, active)
        elif pending_quarantine:
            quarantine(engine, alias, active)
        if not partial.exists() and not pending_quarantine:
            backup.pop("manual_attention_required", None)
        if mirror.exists():
            healthy = engine.git.healthy(mirror)
            integrity["last_checked_at"] = utcnow()
            integrity["status"] = "healthy" if healthy else "corrupt"
            if healthy:
                integrity["repair_attempts"] = 0
                backup.pop("repair_deferred", None)
                if interrupted and active is not None:
                    if active["type"] == "repair":
                        if integrity.get("completed_repair_operation") != active["id"]:
                            repair_result(engine, alias, active["id"], True)
                    elif backup["completed_operation"] != active["id"]:
                        if active.get("git_completed_at"):
                            engine.finish(alias, active)
                        else:
                            backup["status"] = "interrupted"
            else:
                repair(engine, alias)
        elif not partial.exists() and not pending_quarantine:
            backup["status"] = "interrupted"
            integrity["status"] = "unchecked"
            backup.pop("manual_attention_required", None)
        engine.save_meta(alias)
        current = store.data["active_operation"]
        if current and current["alias"] == alias:
            # save_meta above durably retains suspect/repair_deferred before the
            # single journal slot can be reused. Recovery will revisit this repo
            # even after another repository replaces active_operation. A signal
            # still leaves the operation interrupted and stops the next iteration.
            paused_repair = (
                current["type"] == "repair" and current["state"] == "interrupted"
            )
            if paused_repair:
                engine.checkpoint("repair_recovery_metadata")
            if not engine.git.stop.is_set() and current["state"] in (
                "running",
                "interrupted",
            ):
                store.update_operation(state="reconciled", completed_at=utcnow())
                if paused_repair:
                    engine.checkpoint("repair_recovery_journal")
    with store.transaction() as data:
        if data["active_run"] and data["active_run"]["state"] == "running":
            data["active_run"]["state"] = "interrupted"
        data["recovery"]["last_checked_at"] = utcnow()


def repair_result(engine: Engine, alias: str, operation: str, healthy: bool) -> None:
    meta = engine.repos[alias]
    integrity, backup = meta["integrity"], meta["backup"]
    if integrity.get("completed_repair_operation") == operation:
        return
    integrity["completed_repair_operation"] = operation
    integrity["last_repair_at"] = utcnow()
    integrity["last_repair_result"] = "healthy" if healthy else "failed"
    if healthy:
        integrity["status"] = "healthy"
        integrity["successful_repairs"] += 1
        integrity["repair_attempts"] = 0
        backup["status"] = "interrupted"
    else:
        integrity["repair_attempts"] += 1
        record_error(meta, "REPAIR_FAILED")
        if integrity["repair_attempts"] >= engine.config.repair_attempts:
            integrity["status"] = backup["status"] = "repair_failed"
        else:
            integrity["status"] = "corrupt"
            backup["status"] = "repairing"
    engine.save_meta(alias)


def repair(engine: Engine, alias: str) -> None:
    meta = engine.repos[alias]
    integrity, backup = meta["integrity"], meta["backup"]
    candidates = engine.sources if ":" in alias else engine.config.sources
    source = next((s for s in candidates if s.alias == alias), None)
    if (
        source is None
        and ":" in alias
        and meta["selection"]["status"] != "excluded"
        and engine.config.automatic_repair
    ):
        backup["status"] = "repair_paused"
        backup["repair_deferred"] = "DISCOVERY_REQUIRED"
        return
    if (
        source is None
        or meta["selection"]["status"] == "excluded"
        or not engine.config.automatic_repair
    ):
        backup["status"] = "repair_failed"
        return
    capacity = engine.budget.status(alias, engine.path(alias))
    if capacity:
        if integrity["repair_attempts"] < engine.config.repair_attempts:
            backup["status"] = "repair_paused"
            backup["repair_deferred"] = capacity
        else:
            backup["status"] = "repair_failed"
            backup.pop("repair_deferred", None)
        return
    backup.pop("repair_deferred", None)
    mirror = engine.path(alias) / "repository.git"
    while integrity["repair_attempts"] < engine.config.repair_attempts:
        if engine.git.stop.is_set():
            return
        operation = engine.store.begin(alias, "repair")
        integrity["status"] = backup["status"] = "repairing"
        engine.save_meta(alias)
        try:
            engine.git.fetch(
                mirror,
                source.url,
                source.credential,
                engine.budget.monitor(alias, engine.path(alias)),
                repair=True,
            )
            meta["source"]["status"] = "available"
            engine.checkpoint("repair_git")
            healthy = engine.git.healthy(mirror)
        except HarborError as error:
            if isinstance(error, GitError) and error.source_status is not None:
                meta["source"]["status"] = error.source_status
            if engine.git.stop.is_set() or str(error) in {
                "INTERRUPTED",
                "FREE_SPACE_RESERVE",
                "global_limit_paused",
                "repo_limit_reached",
            }:
                integrity["status"] = "suspect"
                integrity["last_repair_result"] = "interrupted"
                backup["status"] = "interrupted"
                engine.save_meta(alias)
                engine.store.update_operation(state="interrupted")
                engine.refresh_storage(alias, after=True)
                return
            healthy = False
        repair_result(engine, alias, operation, healthy)
        engine.checkpoint("repair_metadata")
        engine.store.complete()
        engine.refresh_storage(alias, after=True)
        if healthy:
            return
        if engine.budget.status(alias, engine.path(alias)):
            return
    integrity["status"] = backup["status"] = "repair_failed"
