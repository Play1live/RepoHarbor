"""Read-only status views over atomically published schema-1 state."""

import json
import re
import shutil
import textwrap
from argparse import Namespace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from repoharbor.catalog import catalog_entries, directory, read_metadata
from repoharbor.config import Config, canonical_url
from repoharbor.control import request
from repoharbor.errors import HarborError, Result
from repoharbor.persistence import integrity_state, read_json, validate_state
from repoharbor.retry import view as retry_view
from repoharbor.scheduler import Cron
from repoharbor.selection import repository_id


def timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def number(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def label(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}", value
    ):
        return value
    return None


def error_code(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(
        r"(?:GITHUB_|GITLAB_)[A-Z][A-Z_]{0,50}|GIT_FAILED|INVALID_MIRROR|"
        r"GIT_NETWORK|GIT_SERVER_TEMPORARY|GIT_TIMEOUT|INTERRUPTED|ENOSPC|REPAIR_FAILED|PROVIDER_IDENTITY_CONFLICT|FREE_SPACE_RESERVE",
        value,
    ):
        return value
    return None


def error_event(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    # Error messages are intentionally represented by their stable code.
    return {"at": timestamp(value.get("at")), "code": error_code(value.get("code"))}


def web_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.isprintable():
        return None
    try:
        return canonical_url(value)
    except HarborError:
        return None


def repo_view(
    config: Config, alias: str, meta: dict[str, Any], details: bool
) -> dict[str, Any]:
    repo, selection, source, backup, errors, integrity = (
        meta[k]
        for k in ("repository", "selection", "source", "backup", "errors", "integrity")
    )
    last = error_event(errors["last"])
    saved_periodic = integrity.get("periodic", {})
    periodic = {
        "last_started_at": timestamp(saved_periodic.get("last_started_at")),
        "last_completed_at": timestamp(saved_periodic.get("last_completed_at")),
        "last_result": saved_periodic.get("last_result"),
        "damaged_at": timestamp(saved_periodic.get("damaged_at")),
        "error_code": saved_periodic.get("error_code"),
    }
    result = {
        "id": alias,
        "provider": repo["provider"],
        "name": label(repo.get("name"))
        or (alias if repo["provider"] == "manual" else None),
        "selection_status": label(selection["status"]),
        "source_status": label(source["status"]),
        "backup_status": label(backup["status"]),
        "last_checked_at": timestamp(backup["last_checked_at"]),
        "last_updated_at": timestamp(backup["last_updated_at"]),
        "size_bytes": number(backup["size_bytes"]),
        "last_error_code": last["code"] if last else None,
        "integrity_status": label(integrity["status"]),
        "periodic_integrity": periodic,
        "retry": retry_view(
            meta.get("retry", {}), config.auto_retry, config.retry.max_retries
        ),
    }
    if details:
        provider_host, _, provider_id = alias.rpartition(":")
        owner = repo.get("owner")
        limit = config.storage.overrides.get(alias, config.storage.repo_bytes)
        result.update(
            host=provider_host.partition(":")[2] or None,
            provider_id=provider_id,
            owner=owner
            if isinstance(owner, str) and all(label(part) for part in owner.split("/"))
            else None,
            web_url=web_url(repo["web_url"]),
            visibility=label(repo.get("visibility")),
            selection_reason=label(selection.get("reason")),
            discovery_status=label(source.get("discovery_status")),
            discovery_last_seen_at=timestamp(source.get("last_seen_at")),
            created_at=timestamp(backup["created_at"]),
            last_attempt_at=timestamp(backup["last_attempt_at"]),
            successful_updates=number(backup["successful_updates"]),
            limit_bytes=limit,
            limit_enabled=limit is not None,
            mirror_path=str(
                directory(config.root, alias).relative_to(config.root)
                / "repository.git"
            ),
            errors={
                "total": number(errors["total"]),
                "consecutive": number(errors["consecutive"]),
                "last": last,
                "recent": [error_event(event) for event in errors["recent"]],
            },
            integrity={
                "status": label(integrity["status"]),
                "last_checked_at": timestamp(integrity["last_checked_at"]),
                "last_check_kind": (
                    integrity.get("last_check_kind")
                    if integrity.get("last_check_kind")
                    in ("periodic", "recovery", "repair")
                    else None
                ),
                "repair_attempts": number(integrity["repair_attempts"]),
                "successful_repairs": number(integrity["successful_repairs"]),
                "last_repair_at": timestamp(integrity.get("last_repair_at")),
                "last_repair_result": label(integrity.get("last_repair_result")),
                "repair_deferred": label(backup.get("repair_deferred")),
                "manual_attention_required": label(
                    backup.get("manual_attention_required")
                ),
            },
        )
    return result


def discovery_view(state: dict[str, Any], *, configured: bool) -> dict[str, Any]:
    retry = state.get("retry_at")
    try:
        retry_at = datetime.fromtimestamp(retry, UTC).isoformat() if retry else None
    except (OverflowError, OSError, ValueError):
        retry_at = None
    code = error_code(state.get("code"))
    return {
        "configured": configured,
        "state": state.get("state"),
        "started_at": timestamp(state.get("started_at")),
        "completed_at": timestamp(state.get("completed_at")),
        "error_code": code,
        "error_message": code.removeprefix("GITHUB_")
        .removeprefix("GITLAB_")
        .lower()
        .replace("_", " ")
        if code
        else None,
        "retry_at": retry_at,
    }


def global_view(
    config: Config,
    state: dict[str, Any],
    repos: list[dict[str, Any]],
    unreadable: int | None,
) -> dict[str, Any]:
    live = request(config.root, b"status")
    scheduler = state.get("scheduler", {})
    active = state.get("active_run") or {}
    target = active.get("repository")
    try:
        target = repository_id(target) if isinstance(target, str) else None
    except HarborError:
        target = None
    providers = {f"{p.type}:{p.host}" for p in config.providers}
    saved = dict(state.get("provider_discovery", {}))
    saved["github:github.com"] = state.get("discovery", {})
    now, zone = datetime.now(UTC), ZoneInfo(config.timezone)
    upcoming = Cron(config.schedule).next_after(now, zone)
    next_integrity = (
        Cron(config.integrity.schedule).next_after(now, zone)
        if config.integrity_check
        else None
    )
    periodic = dict(state.get("integrity", integrity_state()))
    if periodic["state"] == "running" and live == Result.SERVICE_UNREACHABLE:
        periodic.update(state="interrupted", current_repository=None)
    storage = state.get("global_storage", {})
    return {
        "service_status": "running"
        if live in (Result.SUCCESS, Result.BUSY)
        else "unreachable",
        "run_status": {Result.SUCCESS: "idle", Result.BUSY: "running"}.get(live),
        "active_run": {
            "started_at": timestamp(active["started_at"]),
            "trigger": label(active.get("trigger")),
            "repository": target,
        }
        if live == Result.BUSY and active.get("state") == "running"
        else None,
        "last_run_started_at": timestamp(scheduler.get("last_run_started_at")),
        "last_run_completed_at": timestamp(scheduler.get("last_run_completed_at")),
        "last_successful_run_at": timestamp(scheduler.get("last_successful_run_at")),
        "next_scheduled_at": upcoming.isoformat() if upcoming else None,
        "timezone": config.timezone,
        "periodic_integrity": {
            "enabled": config.integrity_check,
            "next_scheduled_at": next_integrity.isoformat() if next_integrity else None,
            **periodic,
        },
        "retry": {
            "enabled": config.auto_retry,
            "pending": sum(r["retry"]["pending"] for r in repos)
            if unreadable == 0 or not config.auto_retry
            else None,
        },
        "repositories": {
            "known": len(repos) + unreadable if unreadable is not None else None,
            "included": sum(r["selection_status"] == "included" for r in repos)
            if unreadable == 0
            else None,
            "excluded": sum(r["selection_status"] == "excluded" for r in repos)
            if unreadable == 0
            else None,
            "with_errors": sum(r["has_error"] for r in repos)
            if unreadable == 0
            else None,
            "unreadable": unreadable,
        },
        "storage": {
            "last_measured_bytes": number(storage.get("last_measured_bytes")),
            "last_measured_at": timestamp(storage.get("last_measured_at")),
            "status": label(storage.get("status")),
            "limit_bytes": config.storage.global_bytes,
            "limit_enabled": config.storage.global_bytes is not None,
        },
        "discovery": {
            key: discovery_view(saved.get(key, {}), configured=key in providers)
            for key in sorted(providers | saved.keys())
        },
    }


def output(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=True, allow_nan=False))
        return
    width = max(20, shutil.get_terminal_size().columns)

    def fields(values: dict[str, Any], indent: str = "") -> None:
        for key, value in values.items():
            if isinstance(value, dict):
                print(indent + key.replace("_", " "))
                fields(value, indent + "  ")
                continue
            text = (
                "unknown"
                if value is None
                else json.dumps(value, ensure_ascii=True)
                if isinstance(value, list)
                else str(value)
            )
            print(
                textwrap.fill(
                    f"{key.replace('_', ' ')}  {text}",
                    width,
                    initial_indent=indent,
                    subsequent_indent=indent + "  ",
                )
            )

    if isinstance(data.get("repositories"), list):
        print(f"Repositories  {len(data['repositories'])}")
        for repo in data["repositories"]:
            print()
            fields(repo)
        if data["unreadable"] != 0:
            fields({"unreadable": data["unreadable"]})
    else:
        fields(data)


def print_status(config: Config, args: Namespace) -> Result:
    root = config.root
    try:
        entries = catalog_entries(root)
        unreadable: int | None = 0 if root.is_dir() else None
    except (HarborError, OSError):
        entries = []
        unreadable = None

    if args.command == "repositories" and args.action == "show":
        folder = dict(entries).get(args.repository)
        if folder is None:
            return (
                Result.UNKNOWN_REPOSITORY
                if unreadable == 0
                else Result.STATE_UNAVAILABLE
            )
        try:
            view = repo_view(
                config, args.repository, read_metadata(args.repository, folder), True
            )
        except (HarborError, OSError):
            return Result.STATE_UNAVAILABLE
        output(view, args.json)
        return Result.SUCCESS

    views = []
    for alias, folder in entries:
        try:
            meta = read_metadata(alias, folder)
        except (HarborError, OSError):
            assert unreadable is not None
            unreadable += 1
            continue
        view = repo_view(config, alias, meta, False)
        if args.command == "status":
            backup = meta["backup"]
            view["has_error"] = (
                meta["errors"]["consecutive"] > 0
                or backup["status"]
                in (
                    "error",
                    "interrupted",
                    "repair_failed",
                    "repair_paused",
                    "repairing",
                )
                or meta["integrity"]["status"]
                in ("corrupt", "suspect", "repair_failed", "repairing")
                or view["periodic_integrity"]["last_result"] == "uncheckable"
                or "manual_attention_required" in backup
                or bool(backup.get("repair_deferred"))
                or backup.get("quarantine", {}).get("state") == "pending"
            )
        views.append(view)

    result = Result.SUCCESS if unreadable == 0 else Result.STATE_UNAVAILABLE
    if args.command == "repositories":
        output({"repositories": views, "unreadable": unreadable}, args.json)
        return result
    state: dict[str, Any] = {}
    try:
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise HarborError("Unsafe backup root")
        saved = read_json(root / "data.json")
        validate_state(saved)
        state = saved
    except HarborError:
        result = Result.STATE_UNAVAILABLE
    output(global_view(config, state, views, unreadable), args.json)
    return result
