"""Sequential backup runs; every mutation has a durable journal first."""

import logging
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from repoharbor.catalog import catalog_entries, directory, read_metadata
from repoharbor.config import Config, Source
from repoharbor.errors import GitError, HarborError, Result
from repoharbor.events import Events
from repoharbor.git_backup import Git
from repoharbor.github import DiscoveryError, GitHub
from repoharbor.gitlab import GitLab
from repoharbor.persistence import (
    Store,
    atomic_json,
    metadata,
    record_error,
    safe_tree,
    sync_directory,
    utcnow,
)
from repoharbor.providers import Observation
from repoharbor.registry import (
    gitlab_manual_identities,
    manual_addresses,
    manual_identities,
    plan,
)
from repoharbor.retry import Retries
from repoharbor.selection import address
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
        *,
        retry_clock: Callable[[], float] = time.time,
        events: Events | None = None,
    ):
        self.config, self.store, self.git = config, store, git
        self.events = events if events is not None else Events()
        self.git.backup_lock_fd = store.fd
        self.git.classify_transient = config.auto_retry
        self.retries = Retries(self, retry_clock)
        self.budget = Budget(config.root, config.storage)
        self.checkpoint = checkpoint
        self.repos: dict[str, dict[str, Any]] = {}
        self.sources = config.sources

    def path(self, alias: str) -> Path:
        return directory(self.config.root, alias)

    def save_meta(self, alias: str, *, fatal: bool = False) -> None:
        with self.store.writing(fatal=fatal):
            atomic_json(self.path(alias) / "metadaten.json", self.repos[alias])

    def inventory(self) -> None:
        safe_tree(self.config.root)
        for alias, folder in catalog_entries(self.config.root):
            self.repos[alias] = read_metadata(alias, folder)
        configured = {s.alias: s for s in self.config.sources}
        # Verify identity conflicts for the entire catalog before changing any metadata.
        for alias, meta in self.repos.items():
            if ":" in alias:
                continue
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
            if ":" in alias:
                if not self.config.providers:
                    meta["selection"] = {
                        "status": "excluded",
                        "reason": "provider_disabled",
                    }
                    self.save_meta(alias)
                continue
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

    def discover(self, host_scope: str | None = None) -> set[str]:
        started = self.events.start()
        self.events.log("discovery.started", authority=host_scope)
        failures = set()
        github_config = replace(
            self.config,
            providers=tuple(p for p in self.config.providers if p.type == "github"),
        )
        original = self.config
        if host_scope in (None, "github.com"):
            try:
                self.config = github_config
                if not self.discover_github():
                    failures.add("github:github.com")
            finally:
                self.config = original
        else:
            self.sources = self.manual_sources()
        sources = self.sources
        hosts = {p.host for p in original.providers if p.type == "gitlab"} | {
            m["repository"]["host"]
            for m in self.repos.values()
            if m["repository"]["provider"] == "gitlab"
        }
        for host in sorted(hosts if host_scope is None else hosts & {host_scope}):
            providers = tuple(
                p for p in original.providers if p.type == "gitlab" and p.host == host
            )
            config = replace(original, providers=providers)
            discovered = self.discover_gitlab(config, host)
            if discovered is None:
                failures.add(f"gitlab:{host}")
            else:
                sources += discovered
        self.sources = sources
        self.events.log(
            "discovery.completed",
            started=started,
            authority=host_scope,
            result="partial_failure" if failures else "success",
            considered=len(sources),
        )
        return failures

    def discover_gitlab(self, config: Config, host: str) -> tuple[Source, ...] | None:
        state_key = "gitlab:" + host
        previous = self.store.data.get("provider_discovery", {}).get(state_key, {})
        if config.providers and previous.get("retry_at", 0) > time.time():
            self.events.log("discovery.cooldown", provider="gitlab", authority=host)
            return None
        started = self.events.start()
        self.events.log("discovery.provider_started", provider="gitlab", authority=host)
        with self.store.transaction() as data:
            data.setdefault("provider_discovery", {})[state_key] = {
                "state": "running",
                "started_at": utcnow(),
                "retry_at": previous.get("retry_at", 0),
            }
        client = GitLab(
            self.git.credentials,
            request_delay_ms=config.discovery.request_delay_ms,
            stop=self.git.stop,
        )
        client.bind(host)
        client.retry_at = previous.get("retry_at", 0)

        def persist_cooldown(until: float) -> None:
            with self.store.transaction() as data:
                data["provider_discovery"][state_key]["retry_at"] = until
            self.events.log("discovery.cooldown_set", provider="gitlab", authority=host)

        client.on_cooldown = persist_cooldown
        try:
            observations = []
            for provider in config.providers:
                observations.extend(client.discover(provider))
            manual_ids = (
                gitlab_manual_identities(
                    client, config, manual_addresses(config, self.repos, host), host
                )
                if config.providers
                else set()
            )
            proposed, sources = plan(
                config,
                self.repos,
                observations,
                manual_ids,
                provider_type="gitlab",
                host=host,
            )
        except DiscoveryError as error:
            with self.store.transaction() as data:
                data["provider_discovery"][state_key].update(
                    state="failed",
                    code=str(error),
                    temporary=error.temporary,
                    retry_at=max(error.retry_at, client.retry_at),
                )
            code = str(error).replace("GITHUB_", "GITLAB_")
            self.discovery_failed(
                "gitlab",
                host,
                code,
                error.temporary,
                started,
                ("GitLab discovery skipped: %s", code),
            )
            return None
        with self.store.transaction() as data:
            data["provider_discovery"][state_key]["retry_at"] = client.retry_at
        for alias, meta in proposed.items():
            folder = self.path(alias)
            if alias not in self.repos:
                folder.parent.mkdir(parents=True, exist_ok=True)
                sync_directory(config.root)
                sync_directory(folder.parent.parent)
                folder.mkdir(exist_ok=False)
                sync_directory(folder.parent)
            self.repos[alias] = meta
            self.save_meta(alias)
            self.checkpoint("discovery_metadata")
        with self.store.transaction() as data:
            data["provider_discovery"][state_key].update(
                state="completed",
                completed_at=utcnow(),
                retry_at=client.retry_at,
            )
        self.discovery_completed(
            "gitlab", host, observations, proposed, sources, started
        )
        return sources

    def discover_github(self) -> bool:
        self.sources = self.manual_sources()
        if not self.config.providers and not any(
            k.startswith("github:") for k in self.repos
        ):
            return True
        previous = self.store.data.get("discovery", {})
        if self.config.providers and previous.get("retry_at", 0) > time.time():
            self.events.log(
                "discovery.cooldown", provider="github", authority="github.com"
            )
            return False
        started = self.events.start()
        self.events.log(
            "discovery.provider_started", provider="github", authority="github.com"
        )
        with self.store.transaction() as data:
            data["discovery"] = {"state": "running", "started_at": utcnow()}
        client: GitHub | None = None
        try:
            client = GitHub(
                self.git.credentials,
                request_delay_ms=self.config.discovery.request_delay_ms,
                stop=self.git.stop,
            )
            observations = []
            for provider in self.config.providers:
                if self.git.stop.is_set():
                    raise DiscoveryError("INTERRUPTED")
                observations.extend(client.discover(provider))
            manual_ids = (
                manual_identities(
                    client, self.config, manual_addresses(self.config, self.repos)
                )
                if self.config.providers
                else set()
            )
            proposed, sources = plan(self.config, self.repos, observations, manual_ids)
        except DiscoveryError as error:
            with self.store.transaction() as data:
                data["discovery"].update(
                    state="failed",
                    code=str(error),
                    temporary=error.temporary,
                    retry_at=max(error.retry_at, client.retry_at if client else 0),
                )
            self.discovery_failed(
                "github",
                "github.com",
                str(error),
                error.temporary,
                started,
                ("GitHub discovery skipped: %s", error),
            )
            return False
        if client.retry_at:
            # Keep a known server cooldown even if metadata publication crashes.
            with self.store.transaction() as data:
                data["discovery"]["retry_at"] = client.retry_at
            self.events.log(
                "discovery.cooldown_set", provider="github", authority="github.com"
            )
        # Publish no runnable sources until every metadata write is durable. On a
        # crash, startup performs local recovery only; next discovery recomputes all.
        for alias, meta in proposed.items():
            folder = self.path(alias)
            if alias not in self.repos:
                folder.parent.mkdir(parents=True, exist_ok=True)
                sync_directory(self.config.root)
                sync_directory(folder.parent.parent)
                folder.mkdir(exist_ok=False)
                sync_directory(folder.parent)
            self.repos[alias] = meta
            self.save_meta(alias)
            self.checkpoint("discovery_metadata")
        with self.store.transaction() as data:
            data["discovery"].update(
                state="completed", completed_at=utcnow(), retry_at=client.retry_at
            )
        self.sources = self.manual_sources() + sources
        self.discovery_completed(
            "github",
            "github.com",
            observations,
            proposed,
            sources,
            started,
        )
        return True

    def discovery_failed(
        self,
        provider: str,
        host: str,
        code: str,
        temporary: bool,
        started: float | None,
        legacy: tuple[object, ...],
    ) -> None:
        event = (
            "discovery.rate_limited"
            if code in ("GITHUB_RATE_LIMIT", "GITLAB_RATE_LIMIT")
            else "discovery.failed"
        )
        self.events.log(
            event,
            logging.WARNING,
            legacy=legacy,
            started=started,
            provider=provider,
            authority=host,
            error_code=code,
            temporary=temporary or event == "discovery.rate_limited",
        )

    def discovery_completed(
        self,
        provider: str,
        host: str,
        observations: list[Observation],
        proposed: dict[str, dict[str, Any]],
        sources: tuple[Source, ...],
        started: float | None,
    ) -> None:
        if self.events.logger is not None:
            excluded = sum(
                meta["selection"]["status"] == "excluded" for meta in proposed.values()
            )
            self.events.log(
                "discovery.provider_completed",
                started=started,
                provider=provider,
                authority=host,
                detected=len({item.repository.key for item in observations}),
                considered=len(sources),
                excluded=excluded,
            )

    def discovery_state(self, scope: str) -> dict[str, Any]:
        state: dict[str, Any] = (
            self.store.data.get("discovery", {})
            if scope == "github:github.com"
            else self.store.data.get("provider_discovery", {}).get(scope, {})
        )
        return state

    def provider_mirror_conflict(self, source: Source) -> bool:
        try:
            name = address(source.url)
        except HarborError:
            return False
        return any(
            name in meta["repository"].get("aliases", [])
            and (self.path(key) / "repository.git").exists()
            for key, meta in self.repos.items()
            if ":" in key
        )

    def manual_sources(self) -> tuple[Source, ...]:
        """Do not clone a new manual copy over a known provider-owned URL."""
        result = []
        for source in self.config.sources:
            if not (
                self.path(source.alias) / "repository.git"
            ).exists() and self.provider_mirror_conflict(source):
                self.events.log(
                    "discovery.source_skipped",
                    logging.WARNING,
                    repository=source.alias,
                    error_code="PROVIDER_IDENTITY_CONFLICT",
                    legacy=(
                        "manual source skipped: %s (PROVIDER_IDENTITY_CONFLICT)",
                        source.alias,
                    ),
                )
                continue
            result.append(source)
        return tuple(result)

    def manual_requires_discovery(self, source: Source, failures: set[str]) -> bool:
        if not failures or ":" in source.alias:
            return False
        host = urlsplit(source.url).netloc
        affected = {scope for scope in failures if scope.partition(":")[2] == host}
        if not affected:
            return False
        meta = self.repos[source.alias]
        return (
            meta["selection"]["status"] != "included"
            or meta["repository"]["clone_url"] != source.url
            or not (self.path(source.alias) / "repository.git").is_dir()
            or meta["backup"]["successful_updates"] <= 0
            or self.provider_mirror_conflict(source)
            or any(
                str(self.discovery_state(scope).get("code", "")).endswith(
                    (
                        "_IDENTITY_CONFLICT",
                        "_ALIAS_CONFLICT",
                        "_INCONSISTENT_ID",
                    )
                )
                for scope in affected
            )
        )

    def run(self, *, manual: bool = False, repository: str | None = None) -> Result:
        trigger = "manual" if manual else "scheduled"
        target = repository if repository in self.repos else None
        with self.events.run() as clock:
            self.events.log("backup.run_started", trigger=trigger, repository=target)
            try:
                result = self.run_recorded(manual, repository)
            except Exception:
                self.events.log(
                    "backup.run_failed",
                    logging.ERROR,
                    started=clock,
                    trigger=trigger,
                    repository=target,
                    error_code="BACKUP_FAILED",
                )
                raise
            self.events.log(
                "backup.run_completed",
                started=clock,
                trigger=trigger,
                repository=target,
                result=result.name,
            )
            return result

    def run_recorded(self, manual: bool, repository: str | None) -> Result:
        with self.store.transaction() as data:
            started = utcnow()
            data["active_run"] = {
                "state": "running",
                "started_at": started,
                "trigger": "manual" if manual else "scheduled",
                "repository": repository,
            }
            data["scheduler"]["last_run_started_at"] = started

        result = self.run_backups(repository)
        interrupted = self.git.stop.is_set()
        if interrupted:
            result = Result.BACKUP_FAILED
        with self.store.transaction() as data:
            completed = utcnow()
            data["active_run"].update(
                state="interrupted" if interrupted else "completed", result=result.name
            )
            if not interrupted:
                data["scheduler"]["last_run_completed_at"] = completed
            if result == Result.SUCCESS and repository is None:
                data["scheduler"]["last_successful_run_at"] = completed
        return result

    def run_backups(self, repository: str | None) -> Result:
        from repoharbor.recovery import recover

        safe_tree(self.config.root)
        self.sources = self.manual_sources()
        if repository is None and any(
            (
                (
                    meta["integrity"]["status"] in ("suspect", "repairing")
                    and (self.path(alias) / "repository.git").exists()
                )
                or (self.path(alias) / "repository.git.partial").exists()
                or meta["backup"].get("quarantine", {}).get("state") == "pending"
                or "manual_attention_required" in meta["backup"]
                or meta["backup"]["status"] == "repairing"
                or meta["backup"].get("repair_deferred")
            )
            for alias, meta in self.repos.items()
        ):
            recover(self, during_run=True)

        failures = self.discover()
        if self.config.auto_retry:
            self.retries.cancel_excluded()
        # Keep established manual sources eligible when their authority fails.
        blocked_manual = {
            source.alias
            for source in self.sources
            if self.manual_requires_discovery(source, failures)
        }
        for alias in sorted(blocked_manual):
            self.events.log(
                "discovery.source_skipped",
                repository=alias,
                error_code="DISCOVERY_FAILED",
            )
        self.sources = tuple(s for s in self.sources if s.alias not in blocked_manual)
        if repository is not None:
            provider = repository.rpartition(":")[0]
            if provider in failures or repository in blocked_manual:
                return Result.DISCOVERY_FAILED
            if repository not in self.repos:
                return Result.UNKNOWN_REPOSITORY
            sources = tuple(s for s in self.sources if s.alias == repository)
            if not sources:
                return Result.NOT_SELECTED
            journal = self.store.data["active_operation"]
            if (
                journal
                and journal["alias"] != repository
                and journal["state"] in ("running", "interrupted")
            ):
                # A different repository owns the outstanding journal slot.
                # A full run or startup recovery can safely reconcile it.
                return Result.BACKUP_FAILED
            recover(self, repository=repository, during_run=True, periodic=True)
        else:
            sources = self.sources
            if any(
                ":" in s.alias
                or self.repos[s.alias]["integrity"]
                .get("periodic", {})
                .get("repair_pending")
                for s in sources
            ):
                recover(self, during_run=True, periodic=True)

        failed = False
        for source in sources:
            if self.git.stop.is_set():
                return Result.BACKUP_FAILED
            if self.config.auto_retry and not self.reconcile_operation():
                return Result.BACKUP_FAILED
            if not self.backup(source):
                failed = True
        if failed:
            return Result.BACKUP_FAILED
        if failures and repository is None:
            return Result.DISCOVERY_FAILED
        processed = {source.alias for source in sources}
        if not sources or (
            repository is None
            and any(source.alias not in processed for source in self.config.sources)
        ):
            return Result.NOT_SELECTED
        return Result.SUCCESS

    def reconcile_operation(self, *, retry: bool = False) -> bool:
        """Keep the open backup journal until local recovery can account for it."""
        from repoharbor.recovery import recover

        operation = self.store.data["active_operation"]
        if operation and operation["state"] in ("running", "interrupted"):
            if retry and operation["type"] == "repair":
                return False
            recover(
                self, repository=operation["alias"], during_run=True, allow_repair=False
            )
        operation = self.store.data["active_operation"]
        return not operation or operation["state"] not in ("running", "interrupted")

    def backup(self, source: Source) -> bool:
        alias = source.alias
        clock = self.events.start()
        self.events.log("backup.check_started", repository=alias)
        meta, folder = self.repos[alias], self.path(alias)
        backup = meta["backup"]
        integrity = meta["integrity"]
        if (
            "manual_attention_required" in backup
            or integrity["status"] in ("corrupt", "repair_failed", "suspect")
            or integrity.get("periodic", {}).get("repair_pending")
        ):
            self.events.log(
                "backup.blocked",
                logging.WARNING,
                repository=alias,
                error_code="RECOVERY_REQUIRED",
                started=clock,
            )
            return False
        status = self.refresh_storage(alias)
        if status:
            backup["status"] = status
            self.save_meta(alias)
            self.events.log(
                "backup.storage_blocked",
                logging.WARNING,
                repository=alias,
                error_code=status,
                started=clock,
            )
            return False
        mirror, partial = folder / "repository.git", folder / "repository.git.partial"
        # Unknown partials must be assessed by recovery, never overwritten by clone.
        if partial.exists():
            backup["status"] = "interrupted"
            self.save_meta(alias)
            self.events.log(
                "backup.blocked",
                logging.WARNING,
                repository=alias,
                error_code="RECOVERY_REQUIRED",
                started=clock,
            )
            return False
        backup["last_attempt_at"] = utcnow()
        self.save_meta(alias)
        operation: str | None = None
        try:
            if mirror.exists():
                remote = self.git.remote_refs(source.url, source.credential)
                meta["source"]["status"] = "available"
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
                    meta["errors"]["consecutive"] = 0
                    self.retries.success(alias)
                    self.save_meta(alias)
                    self.events.log(
                        "backup.up_to_date",
                        repository=alias,
                        started=clock,
                        result="unchanged",
                    )
                    return True
                kind = "mirror_update"
            else:
                kind = "initial_clone"
                backup["status"] = "initializing"
                self.save_meta(alias)
            if meta.get("retry", {}).get("status") == "running":
                operation = self.store.begin(alias, kind, retry=True)
            else:
                operation = self.store.begin(alias, kind)
            if kind == "initial_clone":
                backup["partial_operation"] = operation
                self.save_meta(alias)
            self.events.log(
                "backup.clone_started"
                if kind == "initial_clone"
                else "backup.update_started",
                repository=alias,
                operation_id=operation,
            )
            self.checkpoint("journal")

            interrupt = self.budget.monitor(alias, folder)

            if kind == "initial_clone":
                self.git.clone(source.url, partial, source.credential, interrupt)
                self.store.update_operation(git_completed_at=utcnow(), changed=True)
                self.checkpoint("git")
                os.rename(partial, mirror)
                sync_directory(folder)
            else:
                self.git.fetch(mirror, source.url, source.credential, interrupt)
                after = "\n".join(sorted(self.git.refs(mirror).splitlines()))
                self.store.update_operation(
                    git_completed_at=utcnow(), changed=(local != after or head_changed)
                )
                self.checkpoint("git")
            self.checkpoint("promoted")
            self.finish(alias, self.store.data["active_operation"])
            self.checkpoint("metadata")
            self.store.complete()
            self.events.log(
                "backup.completed",
                legacy=("backup completed: %s", alias),
                repository=alias,
                operation_id=operation,
                started=clock,
                result="success",
            )
            return True
        except HarborError as error:
            code = str(error)
            limited = code in {
                "FREE_SPACE_RESERVE",
                "global_limit_paused",
                "repo_limit_reached",
            }
            interrupted = self.git.stop.is_set() or code == "INTERRUPTED"
            backup["status"] = (
                code if limited else "interrupted" if interrupted else "error"
            )
            if isinstance(error, GitError) and error.source_status is not None:
                meta["source"]["status"] = error.source_status
            # Intentional stops/budget pauses are not failed Git attempts.
            if not limited and not interrupted:
                record_error(meta, code)
            if operation and mirror.exists():
                meta["integrity"]["status"] = "suspect"
            retry_status = self.retries.failure(alias, error, limited, interrupted)
            self.save_meta(alias)
            if retry_status == "pending":
                self.checkpoint("retry_planned")
            if operation:
                self.store.update_operation(state="interrupted")
            self.events.log(
                "backup.storage_blocked" if limited else "backup.failed",
                logging.WARNING,
                legacy=("backup failed: %s", alias),
                repository=alias,
                operation_id=operation,
                error_code="INTERRUPTED" if interrupted else code,
                started=clock,
            )
            return False
        finally:
            self.refresh_storage(alias, after=True)

    def refresh_storage(self, alias: str, *, after: bool = False) -> str | None:
        measurement = self.budget.measure(alias, self.path(alias))
        backup = self.repos[alias]["backup"]
        backup["size_bytes"] = measurement.repo_bytes
        backup["limit_bytes"] = self.config.storage.overrides.get(
            alias, self.config.storage.repo_bytes
        )
        backup["storage_status"] = measurement.status or "available"
        with self.store.transaction() as data:
            data["global_storage"] = {
                "status": measurement.global_status or "available",
                "last_measured_bytes": measurement.global_bytes,
                "last_measured_at": measurement.measured_at,
            }
        if after:
            if measurement.status and backup["status"] in ("updated", "up_to_date"):
                backup["status"] = measurement.status
            self.save_meta(alias)
        return measurement.status

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
        if operation["type"] == "initial_clone":
            backup.pop("partial_operation", None)
        meta["source"]["status"] = "available"
        meta["errors"]["consecutive"] = 0
        meta["integrity"]["status"] = "healthy"
        self.retries.success(alias)
        self.save_meta(alias)

    def update_skips(self, skipped: int) -> None:
        with self.store.transaction(fatal=True) as data:
            data["scheduler"]["skipped_overlaps"] += skipped
            data["scheduler"]["last_overlap_at"] = utcnow()
        self.events.log("backup.schedule_skipped", skipped=skipped)
