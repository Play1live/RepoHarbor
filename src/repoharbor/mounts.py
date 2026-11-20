"""Linux mount paths used to detect aliases of protected storage."""

import os
import re
from pathlib import Path
from typing import NamedTuple

from repoharbor.errors import HarborError


class Mount(NamedTuple):
    device: bytes
    root: Path
    id: bytes
    filesystem: bytes


def read_mounts(mountinfo: Path) -> dict[Path, Mount]:
    def decode(value: bytes) -> Path:
        raw = re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), value)
        return Path(os.fsdecode(raw))

    mounts = {}
    for line in mountinfo.read_bytes().splitlines():
        prefix, _, suffix = line.partition(b" - ")
        mount_id, _, device, root, point, *_ = prefix.split()
        filesystem, *_ = suffix.split()
        mounts[decode(point)] = Mount(device, decode(root), mount_id, filesystem)
    return mounts


def overlaps_protected(
    parent: Path,
    protected: tuple[Path, ...],
    mounts: dict[Path, Mount],
    *,
    either_direction: bool = False,
) -> bool:
    def location(path: Path) -> tuple[bytes, Path]:
        for mount in (path, *path.parents):
            if mount in mounts:
                entry = mounts[mount]
                return entry.device, entry.root / path.relative_to(mount)
        raise HarborError("Restore destination mount could not be verified")

    device, relative = location(parent)
    candidates = set(protected)
    candidates.update(p for p in mounts if any(p.is_relative_to(q) for q in protected))
    return any(
        device == other_device
        and (
            relative.is_relative_to(other_path)
            or (either_direction and other_path.is_relative_to(relative))
        )
        for other_device, other_path in map(location, candidates)
    )


def aliases_protected_mount(
    parent: Path, protected: tuple[Path, ...], mountinfo: Path
) -> bool:
    return overlaps_protected(parent, protected, read_mounts(mountinfo))
