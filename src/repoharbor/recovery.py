"""Crash reconciliation and bounded, non-destructive recovery (never backfill)."""

import logging
from pathlib import Path
from typing import Any

from repoharbor.engine import Engine
from repoharbor.errors import GitError, HarborError
from repoharbor.integrity import inspect, record_finding
from repoharbor.persistence import (
    move_new_directory,
    record_error,
    sync_directory,
    sync_tree,
    utcnow,
)


def attention(engine: Engine, alias: str, reason: str) -> None:
    backup = engine.repos[alias]["backup"]
    backup["status"] = "interrupted"
    if backup.get("manual_attention_required") != reason:
        engine.events.log(
            "recovery.manual_attention",
            logging.WARNING,
            repository=alias,
            error_code=reason,
            legacy=("manual attention required: %s (%s)", alias, reason),
        )
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
        if (
            current
            and current["alias"] == alias
            and current["state"] in ("running", "interrupted")
        ):
            # The durable quarantine metadata holds the predecessor operation ID.
            engine.store.update_operation(state="reconciled", completed_at=utcnow())
        engine.store.begin(alias, "quarantine")
    engine.store.update_operation(quarantine_pending=True)
    engine.events.log(
        "recovery.quarantine_started",
        repository=alias,
        operation_id=engine.store.data["active_operation"]["id"],
    )
    engine.checkpoint("quarantine_journal")
    if partial.exists() and destination.exists():
        attention(engine, alias, "QUARANTINE_CONFLICT")
        return
    if partial.exists():
        destination.parent.mkdir(exist_ok=True)
        sync_directory(folder)
        move_new_directory(partial, destination)
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
    engine.events.log(
        "recovery.quarantine_preserved",
        repository=alias,
        operation_id=engine.store.data["active_operation"]["id"],
    )


def verify(
    engine: Engine,
    mirror: Path,
    *,
    integrity: dict[str, Any] | None = None,
    kind: str = "recovery",
) -> bool | None:
    result, code = inspect(engine.git, mirror)
    if result == "interrupted":
        raise HarborError("INTERRUPTED")
    if integrity is not None:
        record_finding(integrity, result, code, kind)
        if result != "healthy":
            integrity.pop("restore_compatibility", None)
    return {"healthy": True, "corrupt": False}.get(result)


def recover(
    engine: Engine,
    *,
    repository: str | None = None,
    during_run: bool = False,
    periodic: bool = False,
    allow_repair: bool = True,
) -> None:
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
        (repository,) if repository is not None else engine.repos,
        key=lambda alias: (not (active and active["alias"] == alias), alias),
    )
    periodic_sources = (
        {source.alias for source in engine.sources} if periodic else set()
    )
    for alias in aliases:
        meta = engine.repos[alias]
        if engine.git.stop.is_set():
            break
        folder = engine.path(alias)
        partial, mirror = folder / "repository.git.partial", folder / "repository.git"
        backup, integrity = meta["backup"], meta["integrity"]
        current = active if active and active["alias"] == alias else None
        interrupted = current is not None and current["state"] in (
            "running",
            "interrupted",
        )
        parked = backup.get("deferred_operation")
        operation = current if interrupted else parked
        recheck_completed = bool(
            parked and parked["state"] == "completed" and not interrupted
        )
        if (
            not during_run
            and current
            and current["state"] == "completed"
            and current["type"] in ("initial_clone", "mirror_update", "repair")
            and not parked
            and (
                (store.data["active_run"] or {}).get("state") == "running"
                or (
                    integrity.get("completed_repair_operation")
                    if current["type"] == "repair"
                    else backup["completed_operation"]
                )
                != current["id"]
                or not mirror.exists()
            )
        ):
            operation = current
            recheck_completed = True
        local_only = not allow_repair or (
            not during_run
            and (
                meta.get("retry", {}).get("status") in ("pending", "running")
                or (
                    interrupted
                    and active is not None
                    and active.get("trigger") == "retry"
                )
            )
        )
        finding = integrity.get("periodic", {})
        periodic_damage = finding.get("repair_pending", False) or (
            integrity.get("damage_pending", False)
            and integrity.get("latest_verification", {}).get("kind") == "manual"
        )
        eligible = (
            alias in periodic_sources and meta["selection"]["status"] == "included"
        )
        pending_quarantine = backup.get("quarantine", {}).get("state") == "pending"
        if not (
            operation is not None
            or (
                (periodic_damage or integrity.get("damage_pending"))
                and eligible
                and mirror.exists()
            )
            or partial.exists()
            or pending_quarantine
            or integrity["status"] in ("suspect", "repairing")
            or backup["status"] == "repairing"
            or "manual_attention_required" in backup
            or backup.get("repair_deferred")
        ):
            continue
        started = engine.events.start()
        operation_id = operation["id"] if operation is not None else None
        partial_exists = partial.exists()
        if interrupted or partial_exists or pending_quarantine:
            engine.events.log(
                "recovery.incomplete_operation",
                repository=alias,
                operation_id=operation_id,
            )
        if partial_exists:
            if (
                interrupted
                and active is not None
                and active.get("git_completed_at")
                and active["type"] == "initial_clone"
                and not mirror.exists()
                and verify(engine, partial) is True
            ):
                sync_tree(partial)
                move_new_directory(partial, mirror)
                sync_directory(folder)
            else:
                quarantine(engine, alias, active)
        elif pending_quarantine:
            quarantine(engine, alias, active)
        expects_mirror = operation is not None and (
            operation["type"] in ("mirror_update", "repair")
            or (operation.get("git_completed_at") and not partial_exists)
        )
        if mirror.exists() or expects_mirror or recheck_completed:
            healthy = verify(engine, mirror, integrity=integrity)
            if healthy is None:
                # Preserve the exact operation in the repository before releasing
                # the shared slot. A crash between these writes leaves both copies.
                if operation is not None:
                    backup["deferred_operation"] = operation.copy()
                if integrity["status"] not in ("corrupt", "repair_failed"):
                    integrity["status"] = "suspect"
                backup.update(
                    status="repair_paused", repair_deferred="RECOVERY_REQUIRED"
                )
                engine.save_meta(alias)
                engine.checkpoint("recovery_deferred_metadata")
                slot = store.data["active_operation"]
                if interrupted and slot and slot["id"] == operation_id:
                    store.update_operation(state="reconciled", completed_at=utcnow())
                engine.events.log(
                    "recovery.deferred",
                    logging.WARNING,
                    repository=alias,
                    error_code="RECOVERY_REQUIRED",
                    started=started,
                    operation_id=operation_id,
                )
                continue
            if parked:
                backup.pop("deferred_operation")
                backup.pop("repair_deferred", None)
                backup["status"] = "interrupted"
            if not partial.exists() and not pending_quarantine:
                backup.pop("manual_attention_required", None)
            integrity.update(last_checked_at=utcnow(), last_check_kind="recovery")
            if healthy or integrity["status"] != "repair_failed":
                integrity["status"] = "healthy" if healthy else "corrupt"
            if healthy is False:
                engine.events.log(
                    "recovery.mirror_damaged",
                    logging.WARNING,
                    repository=alias,
                    error_code="INVALID_MIRROR",
                    operation_id=operation_id,
                )
            if healthy:
                if finding:
                    finding["repair_pending"] = False
                integrity.pop("damage_pending", None)
                if not local_only:
                    integrity["repair_attempts"] = 0
                backup.pop("repair_deferred", None)
                if operation is not None and not recheck_completed:
                    if operation["type"] == "repair":
                        if (
                            integrity.get("completed_repair_operation")
                            != operation["id"]
                        ):
                            # The interrupted Git process may not have reached its
                            # own sync boundary. Persist the verified tree first.
                            sync_tree(mirror)
                            repair_result(engine, alias, operation["id"], True)
                    elif backup["completed_operation"] != operation["id"]:
                        if operation.get("git_completed_at"):
                            engine.finish(alias, operation)
                        else:
                            backup["status"] = "interrupted"
                if (
                    recheck_completed
                    and operation
                    and operation["type"] != "repair"
                    and backup["completed_operation"] != operation["id"]
                ):
                    backup["status"] = "interrupted"
            elif recheck_completed:
                backup["status"] = "interrupted"
            elif local_only:
                backup["repair_deferred"] = "RECOVERY_REQUIRED"
            elif not periodic_damage or eligible:
                repair(engine, alias)
        elif not partial.exists() and not pending_quarantine and not parked:
            if backup["status"] != "error":
                backup["status"] = "interrupted"
            integrity["status"] = "unchecked"
            backup.pop("manual_attention_required", None)
        current = store.data["active_operation"]
        if (
            current
            and current["alias"] == alias
            and current["type"] == "repair"
            and current["state"] == "interrupted"
            and current["id"] != operation_id
        ):
            # A repair started by this recovery pass still needs verification.
            backup["deferred_operation"] = current.copy()
        engine.save_meta(alias)
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
        deferred = backup.get("repair_deferred") or backup.get(
            "manual_attention_required"
        )
        if engine.git.stop.is_set():
            deferred = "INTERRUPTED"
        elif integrity["status"] in ("corrupt", "repair_failed"):
            deferred = deferred or "REPAIR_FAILED"
        engine.events.log(
            "recovery.deferred" if deferred else "recovery.completed",
            logging.WARNING if deferred else logging.INFO,
            repository=alias,
            operation_id=operation_id,
            started=started,
            error_code=deferred,
        )
    if not during_run and not engine.git.stop.is_set():
        engine.retries.reconcile_interrupted()
    with store.transaction() as data:
        if (
            not during_run
            and data["active_run"]
            and data["active_run"]["state"] == "running"
        ):
            data["active_run"]["state"] = "interrupted"
        data["recovery"]["last_checked_at"] = utcnow()


def repair_result(
    engine: Engine,
    alias: str,
    operation: str,
    healthy: bool,
    started: float | None = None,
) -> None:
    meta = engine.repos[alias]
    integrity, backup = meta["integrity"], meta["backup"]
    if integrity.get("completed_repair_operation") == operation:
        return
    integrity["completed_repair_operation"] = operation
    integrity["last_repair_at"] = utcnow()
    integrity["last_repair_result"] = "healthy" if healthy else "failed"
    if healthy:
        integrity["status"] = "healthy"
        if "periodic" in integrity:
            integrity["periodic"]["repair_pending"] = False
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
    engine.events.log(
        "repair.completed" if healthy else "repair.failed",
        logging.INFO if healthy else logging.WARNING,
        repository=alias,
        operation_id=operation,
        started=started,
        error_code=None if healthy else "REPAIR_FAILED",
    )
    if not healthy and integrity["repair_attempts"] >= engine.config.repair_attempts:
        engine.events.log(
            "repair.limit_reached",
            logging.WARNING,
            repository=alias,
            attempts=integrity["repair_attempts"],
            operation_id=operation,
        )


def repair(engine: Engine, alias: str) -> None:
    meta = engine.repos[alias]
    integrity, backup = meta["integrity"], meta["backup"]
    candidates = engine.sources if ":" in alias else engine.manual_sources()
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
        started = engine.events.start()
        current = engine.store.data["active_operation"]
        if (
            current
            and current["alias"] == alias
            and current["state"] in ("running", "interrupted")
        ):
            engine.save_meta(alias)
            engine.store.update_operation(state="reconciled", completed_at=utcnow())
        operation = engine.store.begin(alias, "repair")
        integrity["status"] = backup["status"] = "repairing"
        engine.save_meta(alias)
        engine.events.log(
            "repair.started",
            repository=alias,
            operation_id=operation,
            attempts=integrity["repair_attempts"] + 1,
        )
        try:
            engine.budget.admit_transfer(alias, engine.path(alias))
            engine.git.fetch(
                mirror,
                source.url,
                source.credential,
                engine.budget.monitor(alias, engine.path(alias)),
                repair=True,
            )
            meta["source"]["status"] = "available"
            engine.checkpoint("repair_git")
            healthy = verify(
                engine,
                mirror,
                integrity=integrity,
                kind="repair",
            )
            if healthy is None:
                integrity["status"] = "suspect"
                backup.update(
                    status="repair_paused", repair_deferred="RECOVERY_REQUIRED"
                )
                engine.save_meta(alias)
                engine.store.update_operation(state="interrupted")
                return
            integrity.update(last_checked_at=utcnow(), last_check_kind="repair")
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
                engine.events.log(
                    "repair.deferred",
                    logging.WARNING,
                    repository=alias,
                    operation_id=operation,
                    started=started,
                    error_code="INTERRUPTED"
                    if engine.git.stop.is_set()
                    else str(error),
                )
                return
            if str(error) in (
                "MIRROR_UNSUPPORTED",
                "FILESYSTEM_ERROR",
                "GIT_OUTPUT_LIMIT",
            ):
                record_finding(integrity, "uncheckable", str(error), "repair")
                integrity.pop("restore_compatibility", None)
                backup.update(
                    status="repair_paused", repair_deferred="RECOVERY_REQUIRED"
                )
                engine.save_meta(alias)
                engine.store.update_operation(state="interrupted")
                return
            healthy = False
        if healthy:
            sync_tree(mirror, files=False)
            integrity.pop("damage_pending", None)
        repair_result(engine, alias, operation, healthy is True, started)
        engine.checkpoint("repair_metadata")
        engine.store.complete()
        engine.refresh_storage(alias, after=True)
        if healthy:
            return
        if engine.budget.status(alias, engine.path(alias)):
            return
    integrity["status"] = backup["status"] = "repair_failed"
