"""Soft budgets include all files, including excluded and temporary data."""

import os
import shutil
from pathlib import Path

from repoharbor.config import Storage
from repoharbor.errors import HarborError


def usage(path: Path) -> int:
    total = 0
    for directory, dirs, files in os.walk(path):
        for name in dirs + files:
            p = Path(directory) / name
            if p.is_symlink():
                raise HarborError("Symlink in storage tree")
            if p.is_file():
                total += p.stat().st_size
    return total


class Budget:
    def __init__(self, root: Path, config: Storage):
        self.root, self.config = root, config

    def status(self, alias: str, repo: Path) -> str | None:
        if shutil.disk_usage(self.root).free < self.config.min_free_bytes:
            return "FREE_SPACE_RESERVE"
        if (
            self.config.global_bytes is not None
            and usage(self.root) >= self.config.global_bytes
        ):
            return "global_limit_paused"
        limit = self.config.overrides.get(alias, self.config.repo_bytes)
        if limit is not None and usage(repo) >= limit:
            return "repo_limit_reached"
        return None

    def interrupt(self, alias: str, repo: Path) -> bool:
        status = self.status(alias, repo)
        return status == "FREE_SPACE_RESERVE" or (
            self.config.during_transfer == "abort" and status is not None
        )
