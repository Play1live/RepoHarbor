"""Read-only Git verification of the local mirror catalog."""

import logging
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from repoharbor.config import Config
from repoharbor.engine import Engine
from repoharbor.errors import GitError, HarborError, Result
from repoharbor.events import Events
from repoharbor.git_backup import Git
from repoharbor.persistence import (
    INTEGRITY_COUNTS,
    Store,
    integrity_state,
    safe_tree,
    utcnow,
)

Finding = Literal["healthy", "corrupt", "uncheckable", "interrupted"]


def raise_walk_error(error: OSError) -> None:
    raise error


def inspect(git: Git, mirror: Path) -> tuple[Finding, str | None]:
    error_code = "INVALID_MIRROR"
    try:
        if not stat.S_ISDIR(mirror.lstat().st_mode):
            return "corrupt", error_code
        safe_tree(mirror, onerror=raise_walk_error, interrupt=git.stop.is_set)
        if any(
            (mirror / item).exists()
            for item in (
                "commondir",
                "config.worktree",
                "shallow",
                "objects/info/alternates",
                "objects/info/http-alternates",
                "info/grafts",
            )
        ) or any((mirror / "objects/pack").glob("*.promisor")):
            return "uncheckable", "MIRROR_UNSUPPORTED"
        keys = git.run(
            [
                "config",
                "--file",
                str(mirror / "config"),
                "--no-includes",
                "--null",
                "--name-only",
                "--list",
            ],
            probe="config",
        )
        if any(
            key.startswith(("fsck.", "include.", "includeif."))
            or key in ("extensions.partialclone", "extensions.worktreeconfig")
            or (key.startswith("remote.") and key.endswith(".promisor"))
            for key in keys.lower().split("\0")
        ):
            return "uncheckable", "MIRROR_UNSUPPORTED"
        gitdir = f"--git-dir={mirror}"
        if (
            git.run(
                [gitdir, "rev-parse", "--is-bare-repository"], probe="structure"
            ).strip()
            != "true"
        ):
            return "corrupt", error_code
        error_code = "GIT_CORRUPTION"
        # Git can ignore a pack without an index when no ref currently needs it.
        packs = mirror / "objects" / "pack"
        if any(
            not pack.with_suffix(".idx").is_file() for pack in packs.glob("*.pack")
        ) or any(
            not index.with_suffix(".pack").is_file() for index in packs.glob("*.idx")
        ):
            return "corrupt", error_code
        git.run([gitdir, "fsck", "--full", "--no-dangling"], probe="fsck")
    except GitError as error:
        code = str(error)
        if code == "INTERRUPTED":
            return "interrupted", None
        if code == "GIT_FAILED":
            return "corrupt", error_code
        return "uncheckable", code
    except FileNotFoundError:
        return "uncheckable", "MIRROR_MISSING"
    except (OSError, HarborError):
        if git.stop.is_set():
            return "interrupted", None
        return "uncheckable", "FILESYSTEM_ERROR"
    return "healthy", None


def record_finding(
    integrity: dict[str, Any],
    result: Finding | Literal["skipped"],
    code: str | None,
    kind: str,
) -> None:
    at = utcnow()
    integrity["latest_verification"] = {
        "at": at,
        "result": result,
        "code": code,
        "kind": kind,
    }
    if result == "healthy":
        integrity["last_verified_healthy_at"] = at
    elif result == "corrupt":
        if integrity["status"] in ("unchecked", "healthy", "suspect"):
            integrity["status"] = "corrupt"
        integrity["damage_pending"] = True


def skip_overlap(store: Store, skipped: int, events: Events | None = None) -> None:
    with store.transaction(fatal=True) as data:
        state = data.setdefault("integrity", integrity_state())
        state["skipped_overlaps"] += skipped
        state["last_overlap_at"] = utcnow()
    (events if events is not None else Events()).log(
        "integrity.schedule_skipped",
        skipped=skipped,
        legacy=("scheduled integrity check skipped: another operation is active",),
    )


def reconcile(store: Store, events: Events | None = None) -> None:
    for key in ("integrity", "manual_integrity"):
        state = store.data.get(key)
        if state and state["state"] == "running":
            with store.transaction(fatal=True) as data:
                data[key].update(state="interrupted", current_repository=None)
            if events is not None:
                events.log(
                    "integrity.incomplete_operation",
                    error_code="INTERRUPTED",
                    trigger="manual" if key == "manual_integrity" else None,
                    repository=state.get("target"),
                )


def excluded(config: Config, alias: str, meta: dict[str, Any]) -> bool:
    if meta["selection"]["status"] == "excluded":
        return True
    repo = meta["repository"]
    if repo["provider"] == "manual":
        return False
    if not any(
        p.type == repo["provider"] and p.host == repo["host"] for p in config.providers
    ):
        return True
    # Apply current local filters even before a provider has been discovered.
    return (
        config.selection.reason(
            alias,
            set(repo["aliases"]),
            repo["visibility"],
            repo["fork"],
            repo["archived"],
        )
        is not None
    )


REPAIR_BLOCKS = ("repair_paused", "repair_failed")
CAPACITY_BLOCKS = ("FREE_SPACE_RESERVE", "global_limit_paused", "repo_limit_reached")


def clear_resolved_damage(
    meta: dict[str, Any], folder: Path, journal: dict[str, Any] | None
) -> None:
    """Publish a healthy mirror without reconciling an unfinished write."""
    backup, integrity = meta["backup"], meta["integrity"]
    periodic = integrity.get("periodic", {})
    deferred = backup.get("repair_deferred")
    if (
        integrity["status"]
        not in ("healthy", "unchecked", "corrupt", "suspect", *REPAIR_BLOCKS)
        or backup["status"] in ("initializing", "interrupted", "repairing")
        or "manual_attention_required" in backup
        or (journal and journal["state"] in ("running", "interrupted"))
        or deferred not in (None, "DISCOVERY_REQUIRED", *CAPACITY_BLOCKS)
    ):
        return
    # A partial can exist without metadata. Any filesystem error other than an
    # absent path leaves the operation state unverified.
    try:
        with suppress(FileNotFoundError):
            (folder / "repository.git.partial").lstat()
            return
    except OSError:
        return

    resolved = (
        integrity["status"] in ("corrupt", "suspect", *REPAIR_BLOCKS)
        or periodic.get("repair_pending")
        or backup["status"] in REPAIR_BLOCKS
        or deferred is not None
        or integrity.get("damage_pending")
    )
    integrity["status"] = "healthy"
    integrity.pop("damage_pending", None)
    if not resolved:
        return
    if periodic:
        periodic["repair_pending"] = False
    integrity["repair_attempts"] = 0
    backup.pop("repair_deferred", None)
    if backup["status"] in REPAIR_BLOCKS:
        storage = backup.get("storage_status")
        if storage not in CAPACITY_BLOCKS:
            storage = deferred if deferred in CAPACITY_BLOCKS else "pending"
        backup["status"] = storage

    # Clear the streak only when every failure is attributable to this repair.
    # A mixed or truncated streak can still contain unresolved backup errors.
    errors = meta["errors"]
    count, recent = errors["consecutive"], errors["recent"]
    if 0 < count <= len(recent) and all(
        isinstance(event, dict) and event.get("code") == "REPAIR_FAILED"
        for event in recent[-count:]
    ):
        errors["consecutive"] = 0


def run(
    engine: Engine, *, manual: bool = False, repository: str | None = None
) -> Result:
    if repository is not None and repository not in engine.repos:
        return Result.UNKNOWN_REPOSITORY
    key = "manual_integrity" if manual else "integrity"
    trigger = "manual" if manual else None
    with engine.events.run() as started:
        engine.events.log("integrity.started", trigger=trigger, repository=repository)
        try:
            check_mirrors(engine, manual=manual, repository=repository)
        except Exception:
            engine.events.log(
                "integrity.failed",
                logging.ERROR,
                started=started,
                error_code="FILESYSTEM_ERROR",
                trigger=trigger,
                repository=repository,
            )
            raise
        state = engine.store.data[key]
        outcome = (
            state["last_result"] if state["state"] == "completed" else "interrupted"
        )
        engine.events.log(
            "integrity.completed",
            started=started,
            result=outcome,
            checked=state["checked"],
            damaged=state["corrupt"],
            skipped=state["skipped"],
            trigger=trigger,
            repository=repository,
        )
        return {
            "healthy": Result.SUCCESS,
            "issues": Result.INTEGRITY_ISSUES,
            "no_candidates": Result.INTEGRITY_SKIPPED,
            "interrupted": Result.INTEGRITY_INTERRUPTED,
        }[outcome]


def blocked(alias: str, backup: dict[str, Any], active: dict[str, Any] | None) -> bool:
    return bool(
        backup["created_at"] is None
        or "partial_operation" in backup
        or backup.get("quarantine", {}).get("state") == "pending"
        or (
            active
            and active["alias"] == alias
            and active["type"] in ("initial_clone", "quarantine")
            and active["state"] in ("running", "interrupted")
        )
    )


def has_partial(folder: Path) -> bool:
    try:
        (folder / "repository.git.partial").lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def check_mirrors(
    engine: Engine, *, manual: bool = False, repository: str | None = None
) -> None:
    store = engine.store
    key = "manual_integrity" if manual else "integrity"
    with store.transaction(fatal=True) as data:
        state = data.setdefault(key, integrity_state())
        if manual:
            state["target"] = repository
        history = data.setdefault("integrity_history", {})
        if state["last_completed_at"] is not None and key not in history:
            history[key] = {
                "at": state["last_completed_at"],
                "started_at": state["last_started_at"],
                "result": state["last_result"],
            }
        state.update(last_completed_at=None, last_result=None)
        state.update(
            state="running",
            last_started_at=utcnow(),
            current_repository=None,
            **dict.fromkeys(INTEGRITY_COUNTS, 0),
        )
    active = store.data["active_operation"]
    aliases = sorted(engine.repos) if repository is None else [repository]
    unsafe_skips = 0
    for alias in aliases:
        if engine.git.stop.is_set():
            break
        meta = engine.repos[alias]
        folder = engine.path(alias)
        intentionally_excluded = (
            not manual
            and not engine.config.integrity.include_excluded
            and excluded(engine.config, alias, meta)
        )
        safety_skip = blocked(alias, meta["backup"], active) or has_partial(folder)
        if intentionally_excluded or safety_skip:
            if (
                safety_skip
                and not intentionally_excluded
                and meta["backup"]["created_at"] is not None
            ):
                unsafe_skips += 1
                info = meta["integrity"]
                record_finding(
                    info, "skipped", None, "manual" if manual else "periodic"
                )
                info.pop("restore_compatibility", None)
                engine.save_meta(alias, fatal=True)
            with store.transaction(fatal=True) as data:
                data[key]["skipped"] += 1
            if manual:
                engine.events.log(
                    "integrity.mirror_checked",
                    repository=alias,
                    trigger="manual",
                    result="skipped",
                )
            continue
        with store.transaction(fatal=True) as data:
            data[key]["current_repository"] = alias
        integrity = meta["integrity"]
        record = integrity.setdefault(
            "manual" if manual else "periodic",
            dict(last_completed_at=None, last_result=None, error_code=None),
        )
        if not manual:
            record.setdefault("damaged_at", None)
            record.setdefault("repair_pending", False)
        record["last_started_at"] = utcnow()
        engine.save_meta(alias, fatal=True)
        engine.checkpoint("integrity_started")
        started = engine.events.start()
        result, code = inspect(engine.git, folder / "repository.git")
        if result == "interrupted" or engine.git.stop.is_set():
            reconcile(store)
            return
        finished = utcnow()
        record.update(last_completed_at=finished, last_result=result, error_code=code)
        record_finding(integrity, result, code, "manual" if manual else "periodic")
        if result == "healthy":
            integrity["restore_compatibility"] = engine.git.restore_compatibility(
                folder / "repository.git"
            )
        else:
            integrity.pop("restore_compatibility", None)
        if result == "corrupt" and not manual:
            record["damaged_at"] = finished
            record["repair_pending"] = True
        if result in ("healthy", "corrupt") and not manual:
            integrity.update(last_checked_at=finished, last_check_kind="periodic")
        if (
            result == "healthy"
            and (store.data["active_run"] or {}).get("state") != "running"
        ):
            current = active if active and active["alias"] == alias else None
            clear_resolved_damage(meta, folder, current)
        engine.save_meta(alias, fatal=True)
        engine.checkpoint("integrity_metadata")
        engine.events.log(
            "integrity.mirror_damaged"
            if result == "corrupt"
            else "integrity.mirror_checked",
            logging.DEBUG if result == "healthy" else logging.WARNING,
            repository=alias,
            error_code=code,
            result=result,
            started=started,
            trigger="manual" if manual else None,
        )
        with store.transaction(fatal=True) as data:
            state = data[key]
            state[result] += 1
            state["checked"] += int(result in ("healthy", "corrupt"))
            state["current_repository"] = None
    if engine.git.stop.is_set():
        reconcile(store)
        return
    with store.transaction(fatal=True) as data:
        state = data[key]
        state.update(
            state="completed",
            last_completed_at=utcnow(),
            last_result="issues"
            if state["corrupt"]
            or state["uncheckable"]
            or (state["checked"] and (unsafe_skips or (manual and state["skipped"])))
            else "healthy"
            if state["checked"]
            else "no_candidates",
        )
        data.setdefault("integrity_history", {})[key] = {
            "at": state["last_completed_at"],
            "started_at": state["last_started_at"],
            "result": state["last_result"],
        }
