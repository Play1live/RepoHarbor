"""Single-writer, fail-closed JSON persistence on a trusted local filesystem."""

import fcntl
import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repoharbor.errors import HarborError


def utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def safe_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise HarborError("Backup root must be an existing real directory")
    for parent in root.parents:
        if parent.is_symlink():
            raise HarborError("Symlink in backup root path")
    for directory, dirs, files in os.walk(root):
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
        if path.is_symlink():
            raise ValueError
        value = json.loads(path.read_text())
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError
    except (ValueError, OSError, UnicodeError):
        raise HarborError(
            "Invalid or unreadable state; preserving existing data"
        ) from None
    return value


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
                if "git_completed_at" in value and (
                    not isinstance(value["git_completed_at"], str)
                    or type(value.get("changed")) is not bool
                ):
                    raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HarborError("Invalid journal structure; preserving state") from None


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.fd: int | None = None
        self.data: dict[str, Any] = {}
        self._writer = threading.RLock()

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

    def save(self) -> None:
        with self._writer:
            atomic_json(self.root / "data.json", self.data)

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """Serialize scheduler and worker changes with their durable writes."""
        with self._writer:
            before = deepcopy(self.data)
            try:
                yield self.data
                self.save()
            except BaseException:
                self.data = before
                raise

    def update_operation(self, **values: Any) -> None:
        with self.transaction() as data:
            data["active_operation"].update(values)

    def begin(self, alias: str, kind: str) -> str:
        operation = {
            "id": utcnow(),
            "alias": alias,
            "type": kind,
            "state": "running",
            "started_at": utcnow(),
        }
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
