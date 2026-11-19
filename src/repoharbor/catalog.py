"""Read-only repository inventory shared by startup and the CLI."""

from pathlib import Path
from typing import Any

from repoharbor.config import canonical_url, identifier
from repoharbor.errors import HarborError
from repoharbor.github import DiscoveryError
from repoharbor.persistence import metadata, read_json
from repoharbor.registry import stored_repository
from repoharbor.selection import instance_host, repository_key


def directory(root: Path, alias: str) -> Path:
    if ":" in alias:
        repository_key(alias)
        kind, identity = alias.split(":", 1)
        host, rid = identity.rsplit(":", 1)
        return root / kind / host.replace(":", "~") / rid
    return root / "manual" / identifier(alias)


def children(folder: Path) -> list[Path]:
    if folder.is_symlink():
        raise HarborError("Unsafe repository directory; preserving data")
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return []
    if any(p.is_symlink() or not p.is_dir() for p in entries):
        raise HarborError("Unsafe repository directory; preserving data")
    return entries


def catalog_entries(root: Path) -> list[tuple[str, Path]]:
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise HarborError("Unsafe backup root")
    parent = root / "manual"
    entries = [(identifier(f.name), f) for f in children(parent)]
    github = root / "github"
    for instance in children(github):
        if instance.name != "github.com":
            raise HarborError("Unknown provider directory; preserving data")
        entries.extend(
            (repository_key(f"github:github.com:{f.name}"), f)
            for f in children(instance)
        )
    gitlab = root / "gitlab"
    for instance in children(gitlab):
        host = instance_host(instance.name.replace("~", ":"))
        entries.extend(
            (repository_key(f"gitlab:{host}:{f.name}"), f) for f in children(instance)
        )
    return entries


def read_metadata(alias: str, folder: Path) -> dict[str, Any]:
    meta = read_json(folder / "metadaten.json")
    try:
        # Validate structure before mutation; never reset unknown schemas.
        template = metadata(alias, "https://example.org/repo.git", None, None)
        for section, expected in template.items():
            if isinstance(expected, dict) and (
                not isinstance(meta[section], dict)
                or not set(expected) <= meta[section].keys()
            ):
                raise ValueError
        repo, backup, errors = (
            meta["repository"],
            meta["backup"],
            meta["errors"],
        )
        if ":" in alias:
            stored_repository(meta, alias)
        elif repo["id"] != alias or repo["provider"] != "manual":
            raise ValueError
        if backup["format"] != "mirror":
            raise ValueError
        canonical_url(repo["clone_url"])
        if "partial_operation" in backup and not isinstance(
            backup["partial_operation"], str
        ):
            raise ValueError
        if "quarantine" in backup:
            q = backup["quarantine"]
            if (
                not isinstance(q, dict)
                or q.get("state") not in ("pending", "preserved")
                or not isinstance(q.get("operation_id"), str)
                or q.get("relative_path") != "quarantine/initial-clone"
            ):
                raise ValueError
        if not isinstance(errors["recent"], list) or len(errors["recent"]) > 20:
            raise ValueError
        if any(
            type(errors[k]) is not int or errors[k] < 0
            for k in ("total", "consecutive")
        ):
            raise ValueError
        if (
            type(backup["successful_updates"]) is not int
            or type(meta["integrity"]["repair_attempts"]) is not int
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, DiscoveryError):
        raise HarborError("Invalid repository metadata; preserving data") from None
    return meta
