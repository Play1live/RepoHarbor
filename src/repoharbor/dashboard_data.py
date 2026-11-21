"""Read-only dashboard projections over schema-1 state and structured events."""

import stat
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from repoharbor.catalog import read_metadata
from repoharbor.config import Config
from repoharbor.control import request
from repoharbor.errors import HarborError, Result
from repoharbor.events import ERROR_CODES, Events, Field
from repoharbor.integrity import blocked, has_partial
from repoharbor.persistence import INTEGRITY_COUNTS
from repoharbor.retry import MAX_DISCOVERY_DEFERRALS
from repoharbor.selection import repository_id
from repoharbor.status import (
    global_view,
    has_error,
    inventory,
    label,
    number,
    read_state,
    repo_view,
    timestamp,
)

OPERATIONS = frozenset(
    {
        "backup.blocked",
        "backup.check_started",
        "backup.clone_started",
        "backup.completed",
        "backup.failed",
        "backup.run_completed",
        "backup.run_failed",
        "backup.run_started",
        "backup.schedule_skipped",
        "backup.storage_blocked",
        "backup.up_to_date",
        "backup.update_started",
        "discovery.completed",
        "discovery.cooldown_set",
        "discovery.cooldown",
        "discovery.failed",
        "discovery.provider_completed",
        "discovery.provider_started",
        "discovery.rate_limited",
        "discovery.source_skipped",
        "discovery.started",
        "integrity.completed",
        "integrity.failed",
        "integrity.incomplete_operation",
        "integrity.mirror_checked",
        "integrity.mirror_damaged",
        "integrity.schedule_skipped",
        "integrity.started",
        "recovery.completed",
        "recovery.deferred",
        "recovery.incomplete_operation",
        "recovery.manual_attention",
        "recovery.mirror_damaged",
        "recovery.quarantine_preserved",
        "recovery.quarantine_started",
        "repair.completed",
        "repair.deferred",
        "repair.failed",
        "repair.limit_reached",
        "repair.started",
        "restore.bundle_created",
        "restore.bundle_verified",
        "restore.completed",
        "restore.failed",
        "restore.started",
        "retry.blocked",
        "retry.cooldown_deferred",
        "retry.discovery_deferred",
        "retry.failed",
        "retry.limit_reached",
        "retry.scheduled",
        "retry.started",
        "retry.succeeded",
    }
)


def safe_code(value: object) -> str | None:
    return value if isinstance(value, str) and value in ERROR_CODES else None


def event_view(event: dict[str, Field]) -> dict[str, Field] | None:
    name = event.get("event")
    if not isinstance(name, str) or name not in OPERATIONS:
        return None
    action = name.partition(".")[2]
    code = safe_code(event.get("error_code"))
    result = event.get("result")
    if action.endswith("started"):
        status = "running"
    elif action.endswith(("failed", "damaged")) or result in (
        "BACKUP_FAILED",
        "DISCOVERY_FAILED",
        "issues",
        "uncheckable",
    ):
        status = "error"
    elif (
        code
        or (
            name.startswith("integrity.")
            and result in ("skipped", "no_candidates", "interrupted")
        )
        or action.endswith(("blocked", "skipped", "deferred", "reached"))
        or action
        in (
            "cooldown",
            "cooldown_set",
            "rate_limited",
            "incomplete_operation",
            "manual_attention",
        )
    ):
        status = "warning"
    elif action == "scheduled":
        status = "pending"
    else:
        status = "success"
    return {
        "operation": name,
        "at": timestamp(event.get("timestamp")),
        "repository": event.get("repository"),
        "host": event.get("authority"),
        "status": status,
        "code": code,
        "correlation": event.get("run_id"),
        "duration_ms": event.get("duration_ms"),
        "trigger": "manual"
        if name.startswith("integrity.") and event.get("trigger") == "manual"
        else None,
    }


def settings(config: Config) -> dict[str, Any]:
    return {
        "schedule": config.schedule,
        "timezone": config.timezone,
        "retry": {
            "enabled": config.auto_retry,
            "attempts": config.retry.max_retries,
            "initial_delay": config.retry.initial_delay_seconds,
            "max_delay": config.retry.max_delay_seconds,
            "max_deferrals": MAX_DISCOVERY_DEFERRALS,
        },
        "storage": {
            "global_limit": config.storage.global_bytes,
            "repo_limit": config.storage.repo_bytes,
            "reserve": config.storage.min_free_bytes,
            "during_transfer": config.storage.during_transfer,
            "overrides": config.storage.overrides,
        },
        "integrity": {
            "enabled": config.integrity_check,
            "schedule": config.integrity.schedule,
            "include_excluded": config.integrity.include_excluded,
            "repair": config.automatic_repair,
            "attempts": config.repair_attempts,
        },
        "logging": {
            "enabled": config.extended_logging,
            "level": config.logging.level if config.extended_logging else "INFO",
            "format": config.logging.format if config.extended_logging else "text",
            "file": config.logging.file.enabled,
            "file_size": config.logging.file.max_size_mib,
            "archives": config.logging.file.backup_count,
        },
        "web": {
            "enabled": config.web.enabled,
            "bind": config.web.bind,
            "port": config.web.port,
        },
        "manual_run": config.manual_run,
        "restore": config.restore_helper,
    }


def repository_view(
    config: Config,
    alias: str,
    meta: dict[str, Any],
    manual_host: str | None,
    folder: Path,
    active: dict[str, Any] | None,
) -> dict[str, Any]:
    repo = repo_view(config, alias, meta, True)
    repo.pop("mirror_path")
    repo["host"] = repo["host"] or manual_host
    repo["has_error"] = has_error(meta, repo)
    repo["storage_status"] = label(meta["backup"].get("storage_status"))
    repo["size_measured_at"] = timestamp(meta["backup"].get("size_measured_at"))
    if repo["size_measured_at"] is None and not repo["last_checked_at"]:
        # Inventory creates a zero size before any actual measurement.
        repo["size_bytes"] = None
    errors = repo["errors"]
    for projected, saved in zip(
        [errors["last"], *errors["recent"]],
        [meta["errors"]["last"], *meta["errors"]["recent"]],
        strict=True,
    ):
        if projected is not None:
            projected["code"] = safe_code(saved.get("code"))
    repo["last_error_code"] = errors["last"]["code"] if errors["last"] else None
    for field in ("last_error_code", "block_reason", "reason"):
        repo["retry"][field] = safe_code(repo["retry"][field])
    periodic = repo["periodic_integrity"]
    periodic["error_code"] = safe_code(periodic["error_code"])
    manual = meta["integrity"].get("manual", {})
    repo["manual_integrity"] = {
        "last_started_at": timestamp(manual.get("last_started_at")),
        "last_completed_at": timestamp(manual.get("last_completed_at")),
        "last_result": manual.get("last_result"),
        "error_code": safe_code(manual.get("error_code")),
    }
    repo["can_check_integrity"] = False
    if (
        config.manual_run
        and not blocked(alias, meta["backup"], active)
        and not has_partial(folder)
    ):
        with suppress(OSError):
            repo["can_check_integrity"] = stat.S_ISDIR(
                (folder / "repository.git").lstat().st_mode
            )
    return repo


def snapshot(config: Config, events: Events) -> dict[str, Any]:
    try:
        state = read_state(config)
    except (HarborError, OSError):
        state = {}
    entries, unreadable = inventory(config)
    repos = []
    hosts = {s.alias: urlsplit(s.url).netloc for s in config.sources}
    for alias, folder in entries:
        try:
            repos.append(
                repository_view(
                    config,
                    alias,
                    read_metadata(alias, folder),
                    hosts.get(alias),
                    folder,
                    state.get("active_operation"),
                )
            )
        except (HarborError, OSError, OverflowError, TypeError, ValueError):
            assert unreadable is not None
            unreadable += 1
    live = request(config.root, b"status")
    overall = global_view(config, state, repos, unreadable, live=live)
    if manual := state.get("manual_integrity"):
        overall["manual_integrity"] = {
            "state": "interrupted"
            if manual["state"] == "running" and live == Result.SERVICE_UNREACHABLE
            else manual["state"],
            "target": manual["target"],
            "repository": manual["current_repository"] if live == Result.BUSY else None,
            "started_at": timestamp(manual["last_started_at"]),
            "last_result": manual["last_result"],
            "last_completed_at": timestamp(manual["last_completed_at"]),
            "last_finished_run": state.get("integrity_history", {}).get(
                "manual_integrity"
            ),
            **{key: manual[key] for key in INTEGRITY_COUNTS},
        }
    stored = state.get("global_storage", {})
    overall["storage"]["free_bytes"] = number(stored.get("free_bytes"))
    last_run = (state.get("active_run") or {}).get("state")
    overall["last_backup_state"] = (
        "interrupted"
        if last_run == "running" and live == Result.SERVICE_UNREACHABLE
        else last_run
    )
    operation = state.get("active_operation") or {}
    if live == Result.BUSY and operation.get("state") == "running":
        try:
            alias = repository_id(operation["alias"])
        except HarborError:
            pass
        else:
            if operation.get("trigger") == "retry":
                overall["active_retry"] = {
                    "started_at": timestamp(operation.get("started_at")),
                    "trigger": "retry",
                    "repository": alias,
                }
            elif overall["active_run"]:
                overall["active_run"]["repository"] = alias
    periodic = overall["periodic_integrity"]
    overall["periodic_integrity"] = {
        "enabled": periodic["enabled"],
        "state": label(periodic.get("state")),
        "last_result": label(periodic.get("last_result")),
        "last_completed_at": timestamp(periodic.get("last_completed_at")),
        "started_at": timestamp(periodic.get("last_started_at")),
        "last_finished_run": periodic["last_finished_run"],
        "next_scheduled_at": periodic["next_scheduled_at"],
        "repository": periodic.get("current_repository"),
    }
    discovery = overall.pop("discovery")
    sources = []
    for provider, host in sorted({(p.type, p.host) for p in config.providers}):
        observed = discovery[f"{provider}:{host}"]
        count = [r for r in repos if r["provider"] == provider and r["host"] == host]
        sources.append(
            {
                "provider": provider,
                "host": host,
                "status": observed["state"] or ("never" if state else None),
                "completed_at": observed["completed_at"],
                "started_at": observed["started_at"],
                "retry_at": observed["retry_at"],
                "code": safe_code(observed["error_code"]),
                "known": len(count) if unreadable == 0 else None,
                "included": sum(r["selection_status"] == "included" for r in count)
                if unreadable == 0
                else None,
                "mode": config.selection.mode,
                "visibility": config.selection.visibility,
                "forks": config.selection.include_forks,
                "archived": config.selection.include_archived,
            }
        )
    for host in sorted(set(hosts.values())):
        aliases = {alias for alias, authority in hosts.items() if host == authority}
        sources.append(
            {
                "provider": "manual",
                "host": host,
                "status": "not_required",
                "configured": len(aliases),
                "mode": "explicit",
            }
        )
    return {
        "at": datetime.now(UTC).isoformat(),
        "state_available": bool(state),
        "service": overall,
        "repositories": repos,
        "activities": sorted(
            (event for raw in events.recent_events() if (event := event_view(raw))),
            key=lambda event: str(event["at"]),
            reverse=True,
        ),
        "sources": sources,
        "settings": settings(config),
    }


class DashboardData:
    """Share one bounded-frequency disk snapshot across browser tabs."""

    def __init__(self, config: Config, events: Events):
        self.config = config
        self.events = events
        self.lock = Lock()
        self.expiry = 0.0
        self.refreshing: Event | None = None
        self.generation = 0
        self.saved: dict[str, Any] = {}

    def __call__(self) -> dict[str, Any]:
        with self.lock:
            now = monotonic()
            if now < self.expiry:
                return self.saved
            pending = self.refreshing
            if pending is not None and self.expiry and now - self.expiry <= 5:
                return self.saved
            generation = self.generation
            done = Event()
            if pending is None:
                self.refreshing = done
        if pending is not None:
            if pending.wait(0.75):
                with self.lock:
                    if monotonic() < self.expiry and generation == self.generation:
                        return self.saved
            raise HarborError("Dashboard snapshot is being refreshed")
        try:
            latest = snapshot(self.config, self.events)
            with self.lock:
                if generation != self.generation:
                    raise HarborError("Dashboard snapshot was superseded")
                self.saved = latest
                self.expiry = monotonic() + 5
                return latest
        finally:
            with self.lock:
                self.refreshing = None
                done.set()

    def invalidate(self) -> None:
        with self.lock:
            self.generation += 1
            self.expiry = 0.0
