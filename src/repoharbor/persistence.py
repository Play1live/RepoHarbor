"""Single-writer, fail-closed JSON persistence on a trusted local filesystem."""

import fcntl
import json
import math
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repoharbor.errors import HarborError
from repoharbor.selection import repository_id


def utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def safe_tree(
    root: Path,
    *,
    onerror: Callable[[OSError], None] | None = None,
    interrupt: Callable[[], bool] | None = None,
) -> None:
    if root.is_symlink() or not root.is_dir():
        raise HarborError("Backup root must be an existing real directory")
    for parent in root.parents:
        if parent.is_symlink():
            raise HarborError("Symlink in backup root path")
    for directory, dirs, files in os.walk(root, onerror=onerror):
        if interrupt is not None and interrupt():
            raise HarborError("INTERRUPTED")
        for name in dirs + files:
            mode = (Path(directory) / name).lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise HarborError("Symlink or special file in backup root")


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    if path.is_symlink():
        raise HarborError("Refusing JSON symlink")
    fd, temporary = tempfile.mkstemp(
        prefix=".repoharbor-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path: Path) -> dict[str, Any]:
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags)) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            value = json.load(stream)
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError
    except (ValueError, OSError, RecursionError):
        raise HarborError(
            "Invalid or unreadable state; preserving existing data"
        ) from None
    return value


INTEGRITY_COUNTS = ("checked", "healthy", "corrupt", "skipped", "uncheckable")
PERIODIC_ERRORS = {
    "INVALID_MIRROR",
    "GIT_CORRUPTION",
    "MIRROR_MISSING",
    "MIRROR_UNSUPPORTED",
    "FILESYSTEM_ERROR",
    "GIT_TIMEOUT",
    "GIT_INTERRUPTED",
    "GIT_OUTPUT_LIMIT",
}


def integrity_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "last_started_at": None,
        "last_completed_at": None,
        "last_result": None,
        "current_repository": None,
        "skipped_overlaps": 0,
        "last_overlap_at": None,
        **dict.fromkeys(INTEGRITY_COUNTS, 0),
    }


def validate_manual_integrity_state(value: Any) -> None:
    if not isinstance(value, dict) or "target" not in value:
        raise ValueError
    try:
        if value["target"] is not None:
            if not isinstance(value["target"], str):
                raise ValueError
            repository_id(value["target"])
    except HarborError:
        raise ValueError from None
    validate_integrity_state(
        {key: item for key, item in value.items() if key != "target"}
    )


def validate_manual_integrity_metadata(value: Any) -> None:
    if not isinstance(value, dict) or value.keys() != {
        "last_started_at",
        "last_completed_at",
        "last_result",
        "error_code",
    }:
        raise ValueError
    result = value["last_result"]
    validate_periodic_metadata(
        dict(
            value,
            damaged_at=value["last_completed_at"] if result == "corrupt" else None,
            repair_pending=False,
        )
    )


def validate_integrity_times(value: dict[str, Any], *names: str) -> None:
    for name in names:
        at = value[name]
        if at is not None and (
            not isinstance(at, str)
            or len(at) > 40
            or datetime.fromisoformat(at).utcoffset() is None
        ):
            raise ValueError


def validate_integrity_state(value: Any) -> None:
    if not isinstance(value, dict) or value.keys() != integrity_state().keys():
        raise ValueError
    validate_integrity_times(
        value, "last_started_at", "last_completed_at", "last_overlap_at"
    )
    current = value["current_repository"]
    if (
        value["state"] not in ("idle", "running", "completed", "interrupted")
        or value["last_result"] not in (None, "healthy", "issues", "no_candidates")
        or (value["last_result"] is None) != (value["last_completed_at"] is None)
        or (value["state"] == "idle") != (value["last_started_at"] is None)
        or (value["state"] == "completed" and value["last_completed_at"] is None)
        or (current is not None and value["state"] != "running")
        or any(
            type(value[key]) is not int or value[key] < 0
            for key in (*INTEGRITY_COUNTS, "skipped_overlaps")
        )
        or value["checked"] != value["healthy"] + value["corrupt"]
    ):
        raise ValueError
    if current is not None:
        if not isinstance(current, str):
            raise ValueError
        try:
            repository_id(current)
        except HarborError:
            raise ValueError from None


def validate_periodic_metadata(value: Any) -> None:
    if not isinstance(value, dict) or value.keys() != {
        "last_started_at",
        "last_completed_at",
        "last_result",
        "damaged_at",
        "error_code",
        "repair_pending",
    }:
        raise ValueError
    validate_integrity_times(
        value, "last_started_at", "last_completed_at", "damaged_at"
    )
    result, code = value["last_result"], value["error_code"]
    if (
        value["last_started_at"] is None
        or (result is None) != (value["last_completed_at"] is None)
        or result not in (None, "healthy", "corrupt", "uncheckable")
        or (
            code not in PERIODIC_ERRORS
            if result in ("corrupt", "uncheckable")
            else code is not None
        )
        or type(value["repair_pending"]) is not bool
        or (
            (result == "corrupt" or value["repair_pending"])
            and value["damaged_at"] is None
        )
    ):
        raise ValueError


def validate_state(data: dict[str, Any]) -> None:
    """Reject semantically corrupt state before any startup write."""
    try:
        required = {
            "active_operation",
            "active_run",
            "scheduler",
            "global_storage",
            "recovery",
        }
        if not required <= data.keys():
            raise ValueError
        if "integrity" in data:
            validate_integrity_state(data["integrity"])
        if "manual_integrity" in data:
            validate_manual_integrity_state(data["manual_integrity"])
        scheduler = data["scheduler"]
        if (
            not isinstance(scheduler, dict)
            or type(scheduler.get("skipped_overlaps")) is not int
            or scheduler["skipped_overlaps"] < 0
        ):
            raise ValueError
        if not isinstance(data["global_storage"], dict) or not isinstance(
            data["recovery"], dict
        ):
            raise ValueError
        if "last_checked_at" not in data["recovery"]:
            raise ValueError
        states = data.get("provider_discovery", {})
        if not isinstance(states, dict):
            raise ValueError
        for name in states:
            from repoharbor.selection import instance_host

            if not isinstance(name, str) or not name.startswith("gitlab:"):
                raise ValueError
            instance_host(name.removeprefix("gitlab:"))
        discoveries = list(states.values())
        if "discovery" in data:
            discoveries.append(data["discovery"])
        for discovery in discoveries:
            if (
                not isinstance(discovery, dict)
                or discovery.get("state") not in ("running", "failed", "completed")
                or not isinstance(discovery.get("started_at"), str)
                or (
                    "temporary" in discovery
                    and (
                        type(discovery["temporary"]) is not bool
                        or discovery["state"] != "failed"
                    )
                )
                or type(discovery.get("retry_at", 0)) not in (int, float)
                or not math.isfinite(discovery.get("retry_at", 0))
                or discovery.get("retry_at", 0) < 0
            ):
                raise ValueError
        for key in ("active_run", "active_operation"):
            value = data[key]
            if value is None:
                continue
            if not isinstance(value, dict) or value.get("state") not in {
                "running",
                "completed",
                "interrupted",
                "reconciled",
            }:
                raise ValueError
            if not isinstance(value.get("started_at"), str):
                raise ValueError
            if key == "active_operation":
                if (
                    not isinstance(value.get("alias"), str)
                    or not isinstance(value.get("id"), str)
                    or value.get("type")
                    not in {"initial_clone", "mirror_update", "repair", "quarantine"}
                ):
                    raise ValueError
                if "trigger" in value and (
                    value["trigger"] != "retry"
                    or value["type"] not in ("initial_clone", "mirror_update")
                ):
                    raise ValueError
                if "git_completed_at" in value and (
                    not isinstance(value["git_completed_at"], str)
                    or type(value.get("changed")) is not bool
                ):
                    raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HarborError("Invalid journal structure; preserving state") from None


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.fd: int | None = None
        self.data: dict[str, Any] = {}
        self._writer = threading.RLock()
        self._write_failed = False

    def __enter__(self) -> "Store":
        safe_tree(self.root)
        self.fd = os.open(
            self.root / ".repoharbor.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.root / "data.json"
            if path.exists():
                self.data = read_json(path)
                validate_state(self.data)
            else:
                if set(p.name for p in self.root.iterdir()) - {".repoharbor.lock"}:
                    raise HarborError(
                        "Missing journal in nonempty root; manual inventory required"
                    )
                self.data = {
                    "schema_version": 1,
                    "active_operation": None,
                    "active_run": None,
                    "scheduler": {"skipped_overlaps": 0},
                    "global_storage": {},
                    "recovery": {"last_checked_at": None},
                }
                self.save()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def writing(self, *, fatal: bool = False) -> Iterator[None]:
        with self._writer:
            if self._write_failed:
                raise HarborError("State write failed; restart required")
            try:
                yield
            except BaseException:
                if fatal:
                    self._write_failed = True
                raise

    def save(self, *, fatal: bool = False) -> None:
        with self.writing(fatal=fatal):
            atomic_json(self.root / "data.json", self.data)

    @contextmanager
    def transaction(self, *, fatal: bool = False) -> Iterator[dict[str, Any]]:
        """Serialize scheduler and worker changes with their durable writes."""
        with self._writer:
            if self._write_failed:
                raise HarborError("State write failed; restart required")
            before = deepcopy(self.data)
            try:
                yield self.data
                self.save(fatal=fatal)
            except BaseException:
                self.data = before
                raise

    def update_operation(self, **values: Any) -> None:
        with self.transaction() as data:
            data["active_operation"].update(values)

    def begin(self, alias: str, kind: str, *, retry: bool = False) -> str:
        operation = {
            "id": utcnow(),
            "alias": alias,
            "type": kind,
            "state": "running",
            "started_at": utcnow(),
        }
        if retry:
            operation["trigger"] = "retry"
        with self.transaction() as data:
            data["active_operation"] = operation
        return str(operation["id"])

    def complete(self) -> None:
        self.update_operation(state="completed", completed_at=utcnow())


def metadata(
    alias: str, url: str, web_url: str | None, credential: str | None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": {
            "provider": "manual",
            "id": alias,
            "clone_url": url,
            "web_url": web_url,
        },
        "selection": {"status": "included", "reason": None},
        "source": {"status": "unknown"},
        "backup": {
            "format": "mirror",
            "relative_path": "repository.git",
            "status": "pending",
            "created_at": None,
            "last_attempt_at": None,
            "last_checked_at": None,
            "last_updated_at": None,
            "successful_updates": 0,
            "size_bytes": 0,
            "limit_bytes": None,
            "completed_operation": None,
        },
        "errors": {"total": 0, "consecutive": 0, "last": None, "recent": []},
        "integrity": {
            "status": "unchecked",
            "last_checked_at": None,
            "repair_attempts": 0,
            "successful_repairs": 0,
        },
        "authentication": {"credential_name": credential},
    }


def record_error(meta: dict[str, Any], code: str) -> None:
    error = {"at": utcnow(), "code": code, "message": code}
    errors = meta["errors"]
    errors["total"] += 1
    errors["consecutive"] += 1
    errors["last"] = error
    errors["recent"] = (errors["recent"] + [error])[-20:]
