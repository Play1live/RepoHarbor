"""Descriptor-relative, bounded log rotation on a dedicated Linux mount."""

import fcntl
import logging
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from repoharbor.config import (
    DEFAULT_BACKUP_ROOT,
    DEFAULT_SECRETS_DIR,
    MAX_LOG_ARCHIVES,
    Config,
)
from repoharbor.errors import HarborError
from repoharbor.mounts import overlaps_protected, read_mounts

MOUNTINFO = Path("/proc/self/mountinfo")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK


class LogSetupError(HarborError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def mount_id(fd: int) -> bytes:
    for line in Path(f"/proc/self/fdinfo/{fd}").read_bytes().splitlines():
        if line.startswith(b"mnt_id:"):
            return line.split()[1]
    raise LogSetupError("LOG_MOUNT_UNSAFE", "Log mount could not be verified")


def open_directory(path: Path) -> int:
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise


def log_directory(config: Config) -> tuple[int, bytes]:
    parent = config.logging.file.path.parent
    protected = tuple(
        path.resolve()
        for path in (
            config.root,
            config.secrets_dir,
            DEFAULT_BACKUP_ROOT,
            DEFAULT_SECRETS_DIR,
        )
    )
    mounts = read_mounts(MOUNTINFO)
    if parent not in mounts or parent == Path("/"):
        raise LogSetupError(
            "LOG_MOUNT_MISSING",
            "Log directory must be a dedicated persistent bind mount",
        )
    mount = mounts[parent]
    if mount.filesystem in (b"tmpfs", b"ramfs"):
        raise LogSetupError(
            "LOG_MOUNT_UNSAFE", "Log directory must use persistent storage"
        )
    if any(
        parent.is_relative_to(path) or path.is_relative_to(parent) for path in protected
    ):
        raise LogSetupError(
            "LOG_MOUNT_UNSAFE", "Log directory overlaps protected storage"
        )
    if overlaps_protected(parent, protected, mounts, either_direction=True):
        raise LogSetupError("LOG_MOUNT_UNSAFE", "Log mount overlaps protected storage")
    active = config.logging.file.path
    if any(
        candidate in mounts
        for candidate in (
            active,
            *(active.with_suffix(f".log.{i}") for i in range(1, MAX_LOG_ARCHIVES + 1)),
        )
    ):
        raise LogSetupError(
            "LOG_FILE_UNSAFE", "Log files cannot be separate mount points"
        )
    fd = open_directory(parent)
    try:
        identity = os.fstat(fd)
        if (
            identity.st_uid != os.geteuid()
            or identity.st_mode & 0o022
            or mount_id(fd) != mount.id
        ):
            raise LogSetupError(
                "LOG_MOUNT_UNSAFE",
                "Log directory must be owned by the service and privately writable",
            )
        # All processes using this directory serialize both append and rotation.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, mount.id
    except (HarborError, OSError):
        os.close(fd)
        raise


def validate_file(entry: os.stat_result) -> None:
    if (
        not stat.S_ISREG(entry.st_mode)
        or entry.st_nlink != 1
        or entry.st_uid != os.geteuid()
        or stat.S_IMODE(entry.st_mode) != 0o600
    ):
        raise LogSetupError(
            "LOG_FILE_UNSAFE", "Log files must be private, service-owned regular files"
        )


class RotatingLog(logging.Handler):
    def __init__(self, config: Config, diagnostic: Callable[[], None]):
        super().__init__()
        options = config.logging.file
        self.name_on_disk = options.path.name
        self.maximum = options.max_size_mib * 1024**2
        self.archives = options.backup_count
        self.diagnostic = diagnostic
        self.directory: int | None = None
        self.file: int | None = None
        try:
            self.directory, self.mount = log_directory(config)
            existing = [
                i
                for i in range(1, MAX_LOG_ARCHIVES + 1)
                if self.exists(self.archive(i))
            ]
            self.file = self.open_active()
            for i in existing:
                if i > self.archives:
                    os.unlink(self.archive(i), dir_fd=self.directory)
        except (OSError, ValueError, RuntimeError, HarborError) as error:
            self.close()
            if isinstance(error, LogSetupError):
                raise
            raise LogSetupError(
                "LOG_FILE_UNAVAILABLE",
                "Log directory or file is inaccessible, unsafe, or already in use",
            ) from None

    def archive(self, index: int) -> str:
        return f"{self.name_on_disk}.{index}"

    def exists(self, name: str) -> bool:
        assert self.directory is not None
        try:
            entry = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            return False
        validate_file(entry)
        return True

    def open_active(self) -> int:
        assert self.directory is not None
        flags = FILE_FLAGS
        if not self.exists(self.name_on_disk):
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(self.name_on_disk, flags, 0o600, dir_fd=self.directory)
        try:
            validate_file(os.fstat(fd))
            if mount_id(fd) != self.mount:
                raise LogSetupError(
                    "LOG_FILE_UNSAFE", "Active log must be on the log directory mount"
                )
            return fd
        except (HarborError, OSError):
            os.close(fd)
            raise

    def rotate(self) -> None:
        assert self.directory is not None and self.file is not None
        for i in range(1, self.archives + 1):
            self.exists(self.archive(i))
        if not self.exists(self.name_on_disk) or not os.path.samestat(
            os.stat(self.name_on_disk, dir_fd=self.directory, follow_symlinks=False),
            os.fstat(self.file),
        ):
            raise HarborError("Active log file changed")
        for i in range(self.archives, 0, -1):
            source = self.archive(i - 1) if i > 1 else self.name_on_disk
            if self.exists(source):
                os.replace(
                    source,
                    self.archive(i),
                    src_dir_fd=self.directory,
                    dst_dir_fd=self.directory,
                )
        file, self.file = self.file, None
        os.close(file)
        self.file = self.open_active()

    def emit(self, record: logging.LogRecord) -> None:
        if self.file is None:
            return
        try:
            payload = (self.format(record) + "\n").encode("utf-8")
            size = os.fstat(self.file).st_size
            if size and size + len(payload) > self.maximum:
                self.rotate()
                size = 0
            assert self.file is not None
            if os.write(self.file, payload) != len(payload):
                os.ftruncate(self.file, size)
                raise OSError("Short log write")
        except (OSError, HarborError):
            self.close()
            self.diagnostic()

    def close(self) -> None:
        self.acquire()
        try:
            for fd in (self.file, self.directory):
                if fd is not None:
                    with suppress(OSError):
                        os.close(fd)
            self.file = self.directory = None
            super().close()
        finally:
            self.release()
