"""Read-only periodic verification of the local mirror catalog."""

import logging
import stat
from pathlib import Path
from typing import Any, Literal

from repoharbor.config import Config
from repoharbor.engine import Engine
from repoharbor.errors import GitError, HarborError
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


def skip_overlap(store: Store, skipped: int) -> None:
    with store.transaction(fatal=True) as data:
        state = data.setdefault("integrity", integrity_state())
        state["skipped_overlaps"] += skipped
        state["last_overlap_at"] = utcnow()
    logging.info("scheduled integrity check skipped: another operation is active")


def reconcile(store: Store) -> None:
    state = store.data.get("integrity")
    if state and state["state"] == "running":
        with store.transaction(fatal=True) as data:
            data["integrity"].update(state="interrupted", current_repository=None)


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


def run(engine: Engine) -> None:
    store = engine.store
    with store.transaction(fatal=True) as data:
        state = data.setdefault("integrity", integrity_state())
        state.update(
            state="running",
            last_started_at=utcnow(),
            current_repository=None,
            **dict.fromkeys(INTEGRITY_COUNTS, 0),
        )
    active = store.data["active_operation"]
    for alias, meta in sorted(engine.repos.items()):
        if engine.git.stop.is_set():
            break
        folder = engine.path(alias)
        backup = meta["backup"]
        if (
            (
                not engine.config.integrity.include_excluded
                and excluded(engine.config, alias, meta)
            )
            or backup["created_at"] is None
            or "partial_operation" in backup
            or backup.get("quarantine", {}).get("state") == "pending"
            or (
                active
                and active["alias"] == alias
                and active["type"] in ("initial_clone", "quarantine")
                and active["state"] in ("running", "interrupted")
            )
        ):
            with store.transaction(fatal=True) as data:
                data["integrity"]["skipped"] += 1
            continue
        with store.transaction(fatal=True) as data:
            data["integrity"]["current_repository"] = alias
        integrity = meta["integrity"]
        periodic = integrity.setdefault(
            "periodic",
            dict(
                last_completed_at=None,
                last_result=None,
                damaged_at=None,
                error_code=None,
                repair_pending=False,
            ),
        )
        periodic["last_started_at"] = utcnow()
        engine.save_meta(alias, fatal=True)
        engine.checkpoint("integrity_started")
        result, code = inspect(engine.git, folder / "repository.git")
        if result == "interrupted" or engine.git.stop.is_set():
            reconcile(store)
            return
        finished = utcnow()
        periodic.update(last_completed_at=finished, last_result=result, error_code=code)
        if result in ("healthy", "corrupt"):
            integrity.update(last_checked_at=finished, last_check_kind="periodic")
            if result == "corrupt":
                periodic["damaged_at"] = finished
                if integrity["status"] in ("unchecked", "healthy"):
                    integrity["status"] = "corrupt"
                    periodic["repair_pending"] = True
            elif integrity["status"] == "unchecked":
                integrity["status"] = "healthy"
        engine.save_meta(alias, fatal=True)
        engine.checkpoint("integrity_metadata")
        with store.transaction(fatal=True) as data:
            state = data["integrity"]
            state[result] += 1
            state["checked"] += int(result in ("healthy", "corrupt"))
            state["current_repository"] = None
    if engine.git.stop.is_set():
        reconcile(store)
        return
    with store.transaction(fatal=True) as data:
        state = data["integrity"]
        state.update(
            state="completed",
            last_completed_at=utcnow(),
            last_result="issues"
            if state["corrupt"] or state["uncheckable"]
            else "healthy"
            if state["checked"]
            else "no_candidates",
        )
