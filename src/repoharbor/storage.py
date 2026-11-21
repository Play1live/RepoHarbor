"""Soft budgets and bounded transfer monitoring."""

import os
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from repoharbor.config import Storage
from repoharbor.errors import HarborError
from repoharbor.persistence import utcnow


def walk_error(error: OSError) -> None:
    if not isinstance(error, FileNotFoundError):
        raise error


def usage(path: Path) -> int:
    total = 0
    for directory, dirs, files in os.walk(path, onerror=walk_error):
        for name in dirs + files:
            p = Path(directory) / name
            try:
                if p.is_symlink():
                    raise HarborError("Symlink in storage tree")
                if p.is_file():
                    total += p.stat().st_size
            except FileNotFoundError:
                # Git and atomic journal replacements can remove entries mid-scan.
                continue
    return total


@dataclass(frozen=True)
class Measurement:
    global_bytes: int
    repo_bytes: int
    free_bytes: int
    measured_at: str
    global_status: str | None
    repo_status: str | None
    global_estimate: bool = False
    global_sampled_at: str | None = None

    @property
    def status(self) -> str | None:
        return self.global_status or self.repo_status


class Budget:
    def __init__(self, root: Path, config: Storage):
        self.root, self.config = root, config
        self.batching = False
        self.total: int | None = None
        self.sample_at = 0.0
        self.sample_timestamp: str | None = None
        self.last_repo: tuple[Path, int] | None = None

    @contextmanager
    def batch(self) -> Iterator[None]:
        # The engine owns the exclusive writer lock. Account for changes to its
        # current repository between periodic whole-root measurements.
        self.batching = True
        try:
            yield
        finally:
            self.batching = False
            self.total = None
            self.last_repo = None
            self.sample_timestamp = None

    def global_usage(
        self, repo: Path, size: int, *, exact: bool = False
    ) -> tuple[int, bool]:
        if not self.batching:
            return usage(self.root), False
        now = time.monotonic()
        if self.total is not None and self.last_repo and repo == self.last_repo[0]:
            self.total += size - self.last_repo[1]
        self.last_repo = (repo, size)
        limit = self.config.global_bytes
        if (
            exact
            or self.total is None
            or now >= self.sample_at + 30
            or (limit is not None and self.total >= limit - 16 * 1024**2)
        ):
            self.total = usage(self.root)
            self.sample_at = now
            self.sample_timestamp = utcnow()
            return self.total, False
        return self.total, True

    def measure(self, alias: str, repo: Path, *, exact: bool = False) -> Measurement:
        repo_bytes = usage(repo)
        global_bytes, estimate = self.global_usage(repo, repo_bytes, exact=exact)
        free = shutil.disk_usage(self.root).free
        global_status = None
        if free < self.config.min_free_bytes:
            global_status = "FREE_SPACE_RESERVE"
        elif (
            self.config.global_bytes is not None
            and global_bytes >= self.config.global_bytes
        ):
            global_status = "global_limit_paused"
        limit = self.config.overrides.get(alias, self.config.repo_bytes)
        repo_status = (
            "repo_limit_reached" if limit is not None and repo_bytes >= limit else None
        )
        return Measurement(
            global_bytes,
            repo_bytes,
            free,
            utcnow(),
            global_status,
            repo_status,
            estimate,
            self.sample_timestamp,
        )

    def status(self, alias: str, repo: Path) -> str | None:
        return self.measure(alias, repo).status

    def admit_transfer(self, alias: str, repo: Path) -> None:
        # Status measurements can use an estimate. A configured global limit
        # must also count journal growth and changes outside the current repo.
        status = self.measure(
            alias, repo, exact=self.config.global_bytes is not None
        ).status
        if status:
            raise HarborError(status)

    def transfer_status(self, alias: str, repo: Path) -> str | None:
        if self.config.during_transfer == "finish":
            # Finish ignores soft budgets; statvfs is enough for the physical reserve.
            return (
                "FREE_SPACE_RESERVE"
                if shutil.disk_usage(self.root).free < self.config.min_free_bytes
                else None
            )
        return self.status(alias, repo)

    def interrupt(self, alias: str, repo: Path) -> bool:
        return self.transfer_status(alias, repo) is not None

    def monitor(
        self,
        alias: str,
        repo: Path,
        *,
        interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> Callable[[], bool]:
        """Git polls signals at 100ms; capacity checks occur at most every 5 seconds."""
        next_check = 0.0

        def check() -> bool:
            nonlocal next_check
            now = clock()
            if now >= next_check:
                next_check = now + interval
                status = self.transfer_status(alias, repo)
                if status:
                    raise HarborError(status)
            return False

        return check
