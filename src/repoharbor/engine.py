"""Sequential backup runs; every mutation has a durable journal first."""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from repoharbor.config import Config, Source, canonical_url, identifier
from repoharbor.errors import HarborError
from repoharbor.git_backup import Git
from repoharbor.persistence import (
    Store,
    atomic_json,
    metadata,
    read_json,
    record_error,
    safe_tree,
    sync_directory,
    utcnow,
)
from repoharbor.storage import Budget, usage

Checkpoint = Callable[[str], None]


def no_checkpoint(_: str) -> None:
    pass


class Engine:
    def __init__(
        self,
        config: Config,
        store: Store,
        git: Git,
        checkpoint: Checkpoint = no_checkpoint,
    ):
        self.config, self.store, self.git = config, store, git
        self.budget = Budget(config.root, config.storage)
        self.checkpoint = checkpoint
        self.repos: dict[str, dict[str, Any]] = {}

    def path(self, alias: str) -> Path:
        return self.config.root / "manual" / identifier(alias)

    def save_meta(self, alias: str) -> None:
        atomic_json(self.path(alias) / "metadaten.json", self.repos[alias])

    def inventory(self) -> None:
        safe_tree(self.config.root)
        parent = self.config.root / "manual"
        if parent.exists():
            for folder in sorted(parent.iterdir()):
                alias = identifier(folder.name)
                meta = read_json(folder / "metadaten.json")
                try:
                    # Validate structure before mutation; never reset unknown schemas.
                    template = metadata(
                        alias, "https://example.org/repo.git", None, None
                    )
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
                    if (
                        repo["id"] != alias
                        or repo["provider"] != "manual"
                        or backup["format"] != "mirror"
                    ):
                        raise ValueError
                    canonical_url(repo["clone_url"])
                    if (
                        not isinstance(errors["recent"], list)
                        or len(errors["recent"]) > 20
                    ):
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
                except (KeyError, TypeError, ValueError):
                    raise HarborError(
                        "Invalid repository metadata; preserving data"
                    ) from None
                self.repos[alias] = meta
        configured = {s.alias: s for s in self.config.sources}
        # Verify identity conflicts for the entire catalog before changing any metadata.
        for alias, meta in self.repos.items():
            if (
                alias in configured
                and meta["repository"]["clone_url"] != configured[alias].url
            ):
                raise HarborError("Alias already belongs to another URL")
            if any(
                s.url == meta["repository"]["clone_url"] and s.alias != alias
                for s in configured.values()
            ):
                raise HarborError(
                    "Existing URL has another alias; keep its stable alias"
                )
        for alias, meta in self.repos.items():
            included = alias in configured
            meta["selection"] = {
                "status": "included" if included else "excluded",
                "reason": None if included else "removed_from_config",
            }
            self.save_meta(alias)
        for source in self.config.sources:
            if source.alias not in self.repos:
                folder = self.path(source.alias)
                folder.mkdir(parents=True, exist_ok=False)
                self.repos[source.alias] = metadata(
                    source.alias, source.url, source.web_url, source.credential
                )
            self.repos[source.alias]["authentication"]["credential_name"] = (
                source.credential
            )
            self.repos[source.alias]["repository"]["web_url"] = source.web_url
            self.save_meta(source.alias)

    def run(self) -> None:
        safe_tree(self.config.root)
        self.store.data["active_run"] = {"state": "running", "started_at": utcnow()}
        self.store.data["scheduler"]["last_run_started_at"] = utcnow()
        self.store.save()
        for source in self.config.sources:
            if self.git.stop.is_set():
                break
            self.backup(source)
        self.store.data["active_run"]["state"] = (
            "interrupted" if self.git.stop.is_set() else "completed"
        )
        self.store.data["scheduler"]["last_run_completed_at"] = utcnow()
        self.store.save()

    def backup(self, source: Source) -> None:
        alias = source.alias
        meta, folder = self.repos[alias], self.path(alias)
        backup = meta["backup"]
        if meta["integrity"]["status"] in ("corrupt", "repair_failed", "suspect"):
            return
        status = self.budget.status(alias, folder)
        backup["limit_bytes"] = self.config.storage.overrides.get(
            alias, self.config.storage.repo_bytes
        )
        self.store.data["global_storage"] = {
            "status": status or "available",
            "last_measured_bytes": usage(self.config.root),
            "last_measured_at": utcnow(),
        }
        self.store.save()
        if status:
            backup["status"] = status
            self.save_meta(alias)
            return
        mirror, partial = folder / "repository.git", folder / "repository.git.partial"
        # Unknown partials must be assessed by recovery, never overwritten by clone.
        if partial.exists():
            backup["status"] = "interrupted"
            self.save_meta(alias)
            return
        backup["last_attempt_at"] = utcnow()
        self.save_meta(alias)
        operation: str | None = None
        try:
            if mirror.exists():
                remote = self.git.remote_refs(source.url, source.credential)
                local = "\n".join(sorted(self.git.refs(mirror).splitlines()))
                local_head = self.git.head(mirror)
                head_changed = (
                    self.git.remote_head is not None
                    and local_head != self.git.remote_head
                )
                if remote == local and not head_changed:
                    backup["last_checked_at"] = utcnow()
                    backup["status"] = "up_to_date"
                    meta["source"]["status"] = "available"
                    self.save_meta(alias)
                    return
                kind = "mirror_update"
            else:
                kind = "initial_clone"
                backup["status"] = "initializing"
                self.save_meta(alias)
            operation = self.store.begin(alias, kind)
            self.checkpoint("journal")

            def interrupt() -> bool:
                return self.budget.interrupt(alias, folder)

            if kind == "initial_clone":
                self.git.clone(source.url, partial, source.credential, interrupt)
                self.store.data["active_operation"].update(
                    git_completed_at=utcnow(), changed=True
                )
                self.store.save()
                self.checkpoint("git")
                os.rename(partial, mirror)
                sync_directory(folder)
            else:
                self.git.fetch(mirror, source.url, source.credential, interrupt)
                after = "\n".join(sorted(self.git.refs(mirror).splitlines()))
                self.store.data["active_operation"].update(
                    git_completed_at=utcnow(), changed=(local != after or head_changed)
                )
                self.store.save()
                self.checkpoint("git")
            self.checkpoint("promoted")
            self.finish(alias, self.store.data["active_operation"])
            self.checkpoint("metadata")
            self.store.complete()
            logging.info("backup completed: %s", alias)
        except HarborError as error:
            backup["status"] = "interrupted" if self.git.stop.is_set() else "error"
            meta["source"]["status"] = "unavailable"
            # Only internal error codes cross the Git boundary.
            record_error(meta, str(error))
            if mirror.exists():
                meta["integrity"]["status"] = "suspect"
            self.save_meta(alias)
            if operation:
                self.store.data["active_operation"]["state"] = "interrupted"
                self.store.save()
            logging.warning("backup failed: %s", alias)

    def finish(self, alias: str, operation: dict[str, Any]) -> None:
        """Replay a durably proven Git completion exactly once."""
        meta = self.repos[alias]
        backup = meta["backup"]
        if backup["completed_operation"] == operation["id"]:
            return
        now = operation["git_completed_at"]
        backup.update(
            {
                "created_at": backup["created_at"] or now,
                "last_checked_at": now,
                "status": "updated" if operation["changed"] else "up_to_date",
                "completed_operation": operation["id"],
                "size_bytes": usage(self.path(alias)),
            }
        )
        if operation["changed"]:
            backup["last_updated_at"] = now
            backup["successful_updates"] += 1
        meta["source"]["status"] = "available"
        meta["errors"]["consecutive"] = 0
        meta["integrity"]["status"] = "healthy"
        self.save_meta(alias)

    def update_skips(self, skipped: int) -> None:
        self.store.data["scheduler"]["skipped_overlaps"] += skipped
        self.store.save()
