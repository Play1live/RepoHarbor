"""Soft budgets and bounded transfer monitoring; no caching of inventory results."""

import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from repoharbor.config import Storage
from repoharbor.errors import HarborError
from repoharbor.persistence import utcnow


def walk_error(error: OSError) -> None:
    raise error


def usage(path: Path) -> int:
    total = 0
    for directory, dirs, files in os.walk(path, onerror=walk_error):
        for name in dirs + files:
            p = Path(directory) / name
            if p.is_symlink():
                raise HarborError("Symlink in storage tree")
            if p.is_file():
                total += p.stat().st_size
    return total


@dataclass(frozen=True)
class Measurement:
    global_bytes: int
    repo_bytes: int
    free_bytes: int
    measured_at: str
    global_status: str | None
    repo_status: str | None

    @property
    def status(self) -> str | None:
        return self.global_status or self.repo_status


class Budget:
    def __init__(self, root: Path, config: Storage):
        self.root, self.config = root, config

    def measure(self, alias: str, repo: Path) -> Measurement:
        global_bytes, repo_bytes = usage(self.root), usage(repo)
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
            global_bytes, repo_bytes, free, utcnow(), global_status, repo_status
        )

    def status(self, alias: str, repo: Path) -> str | None:
        return self.measure(alias, repo).status

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
