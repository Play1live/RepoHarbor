"""Schema-1 repository retry state and its read-only status projection."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from repoharbor.config import MAX_RETRIES
from repoharbor.errors import TRANSIENT_GIT_ERRORS, GitError, HarborError
from repoharbor.persistence import safe_tree

if TYPE_CHECKING:
    from repoharbor.engine import Engine

MAX_DISCOVERY_DEFERRALS = 3
DISCOVERY_BLOCKS = ("DISCOVERY_FAILED", "DISCOVERY_LIMIT_REACHED")
DISCOVERY_WAITS = ("DISCOVERY_TEMPORARY", "PROVIDER_COOLDOWN")
BLOCK_REASONS = (
    "BACKUP_FAILED",
    *DISCOVERY_BLOCKS,
    *DISCOVERY_WAITS,
    "NOT_SELECTED",
    "RECOVERY_REQUIRED",
    "INTERRUPTED",
    "FREE_SPACE_RESERVE",
    "global_limit_paused",
    "repo_limit_reached",
)
ATTEMPT_ERRORS = (
    *TRANSIENT_GIT_ERRORS,
    *BLOCK_REASONS,
    "GIT_FAILED",
    "ENOSPC",
    "INVALID_MIRROR",
)
FIELDS = {
    "status",
    "attempts",
    "reason",
    "next_attempt_at",
    "last_attempt_at",
    "last_result",
    "last_error_code",
    "block_reason",
}


def validate_retry(value: Any) -> None:
    if not isinstance(value, dict) or set(value) - {"discovery_deferrals"} != FIELDS:
        raise ValueError
    status, attempts = value["status"], value["attempts"]
    deferrals = value.get("discovery_deferrals", 0)
    if (
        status not in ("pending", "running", "succeeded", "blocked", "exhausted")
        or type(attempts) is not int
        or not 0 <= attempts <= MAX_RETRIES
        or type(deferrals) is not int
        or not 0 <= deferrals <= MAX_DISCOVERY_DEFERRALS
        or value["reason"] not in TRANSIENT_GIT_ERRORS
        or value["last_result"] not in (None, "succeeded", "failed", "interrupted")
        or value["last_error_code"] not in (None, *ATTEMPT_ERRORS)
        or value["block_reason"] not in (None, *BLOCK_REASONS)
    ):
        raise ValueError
    due, last = value["next_attempt_at"], value["last_attempt_at"]
    for stamp in (due, last):
        if stamp is not None and (
            type(stamp) not in (int, float) or not 0 < stamp <= 253402300799
        ):
            raise ValueError
    if (
        (status == "pending") != (due is not None)
        or (status in ("running", "exhausted") and attempts == 0)
        or (status == "succeeded" and attempts != 0)
        or (attempts > 0 and last is None)
        or (due is not None and last is not None and due <= last)
        or (status == "running" and value["last_result"] is not None)
        or (
            status == "pending"
            and value["block_reason"] not in (None, *DISCOVERY_WAITS)
        )
        or (
            value["block_reason"] == "DISCOVERY_TEMPORARY"
            and (status != "pending" or deferrals == 0)
        )
        or (
            value["block_reason"] == "DISCOVERY_LIMIT_REACHED"
            and (status != "blocked" or deferrals != MAX_DISCOVERY_DEFERRALS)
        )
        or (status == "succeeded" and deferrals != 0)
        or (status == "blocked" and value["block_reason"] is None)
        or (status not in ("blocked", "pending") and value["block_reason"] is not None)
        or (value["last_result"] is not None and last is None)
        or (value["last_result"] in (None, "succeeded"))
        != (value["last_error_code"] is None)
    ):
        raise ValueError


def view(state: dict[str, Any], enabled: bool, maximum: int) -> dict[str, Any]:
    def iso(seconds: float | None) -> str | None:
        return datetime.fromtimestamp(seconds, UTC).isoformat() if seconds else None

    status = state.get("status", "idle")
    attempts = state.get("attempts", 0)
    exhausted = status == "exhausted" or attempts >= maximum
    pending = enabled and status == "pending" and not exhausted
    deferred = pending and state.get("block_reason") in DISCOVERY_WAITS
    due = iso(state.get("next_attempt_at"))
    return {
        "status": status,
        "pending": pending,
        "attempts": attempts,
        "max_retries": maximum,
        "next_attempt_at": due,
        "discovery_deferred": deferred,
        "next_discovery_check_at": due if deferred else None,
        "discovery_deferrals": state.get("discovery_deferrals", 0),
        "max_discovery_deferrals": MAX_DISCOVERY_DEFERRALS,
        "reason": state.get("reason"),
        "last_attempt_at": iso(state.get("last_attempt_at")),
        "last_result": state.get("last_result"),
        "last_error_code": state.get("last_error_code"),
        "block_reason": state.get("block_reason"),
        "limit_reached": exhausted,
    }


class Retries:
    """The service's shared execution lock protects every runtime transition."""

    def __init__(self, engine: "Engine", clock: Callable[[], float]):
        self.engine = engine
        self.clock = clock

    def due(self) -> str | None:
        engine = self.engine
        if not engine.config.auto_retry or engine.git.stop.is_set():
            return None
        now = self.clock()
        ready = (
            (state["next_attempt_at"], alias)
            for alias, meta in engine.repos.items()
            if (state := meta.get("retry")) is not None
            and state["status"] == "pending"
            and state["next_attempt_at"] <= now
        )
        earliest = min(ready, default=None)
        return earliest[1] if earliest else None

    def success(self, alias: str) -> None:
        state = self.engine.repos[alias].get("retry")
        if state:
            if state["status"] == "running":
                self.engine.events.log(
                    "retry.succeeded",
                    repository=alias,
                    attempts=state["attempts"],
                    legacy=("retry succeeded repository=%s", alias),
                )
            state.pop("discovery_deferrals", None)
            state.update(
                status="succeeded",
                attempts=0,
                next_attempt_at=None,
                block_reason=None,
                last_result="succeeded"
                if state["last_attempt_at"] is not None
                else None,
                last_error_code=None,
            )

    def failure(
        self, alias: str, error: HarborError, limited: bool, interrupted: bool
    ) -> str | None:
        engine = self.engine
        if not engine.config.auto_retry:
            return None
        meta = engine.repos[alias]
        state = meta.get("retry")
        code = str(error)
        temporary = (
            isinstance(error, GitError)
            and code in TRANSIENT_GIT_ERRORS
            and not limited
            and not interrupted
        )
        if state and state["status"] == "running":
            outcome = (
                "INTERRUPTED"
                if interrupted
                else (code if code in ATTEMPT_ERRORS else "BACKUP_FAILED")
            )
            self.failed_attempt(alias, state, outcome)
        if not temporary:
            reason = (
                "INTERRUPTED" if interrupted else code if limited else "BACKUP_FAILED"
            )
            self.block(alias, reason, save=False)
            return None

        state = meta.setdefault(
            "retry",
            {
                "attempts": 0,
                "last_attempt_at": None,
                "last_result": None,
                "last_error_code": None,
            },
        )
        if (
            state.get("status") == "exhausted"
            or state.get("block_reason") in DISCOVERY_BLOCKS
        ):
            return None
        state["reason"] = code
        if state.get("status") != "pending":
            state["block_reason"] = None
        if state["attempts"] >= engine.config.retry.max_retries:
            self.exhaust(alias, state)
        else:
            # Another scheduled or manual failure must not postpone an existing retry.
            due = state.get("next_attempt_at") or (
                max(self.clock(), state["last_attempt_at"] or 0)
                + engine.config.retry.delay(state["attempts"])
            )
            state.update(status="pending", next_attempt_at=due)
            engine.events.log(
                "retry.scheduled",
                repository=alias,
                error_code=code,
                attempts=state["attempts"],
                due_at=due,
                legacy=(
                    "retry scheduled repository=%s reason=%s attempts=%d due=%s",
                    alias,
                    code,
                    state["attempts"],
                    due,
                ),
            )
        return str(state["status"])

    def failed_attempt(self, alias: str, state: dict[str, Any], code: str) -> None:
        state.update(
            last_result="interrupted" if code == "INTERRUPTED" else "failed",
            last_error_code=code,
        )
        self.engine.events.log(
            "retry.failed",
            logging.WARNING,
            repository=alias,
            error_code=code,
            attempts=state["attempts"],
            legacy=("retry failed repository=%s reason=%s", alias, code),
        )
        if code == "INTERRUPTED":
            self.block(alias, code, save=False)

    def block(self, alias: str, reason: str, *, save: bool = True) -> None:
        engine = self.engine
        state = engine.repos[alias].get("retry")
        if state and state["status"] in ("pending", "running"):
            state.update(status="blocked", next_attempt_at=None, block_reason=reason)
            if save:
                engine.save_meta(alias)
            engine.events.log(
                "retry.blocked",
                logging.WARNING,
                repository=alias,
                error_code=reason,
                legacy=("retry blocked repository=%s reason=%s", alias, reason),
            )

    def exhaust(self, alias: str, state: dict[str, Any]) -> None:
        state.update(status="exhausted", next_attempt_at=None, block_reason=None)
        self.engine.events.log(
            "retry.limit_reached",
            logging.WARNING,
            repository=alias,
            attempts=state["attempts"],
            legacy=(
                "retry limit reached repository=%s attempts=%d",
                alias,
                state["attempts"],
            ),
        )

    def cancel_excluded(self) -> None:
        for alias, meta in self.engine.repos.items():
            if meta["selection"]["status"] == "excluded":
                self.block(alias, "NOT_SELECTED")

    def reconcile_interrupted(self) -> None:
        # Recovery has already published any proven completion via Engine.finish.
        # An unproven claim is consumed once and never repeated automatically.
        for alias, meta in self.engine.repos.items():
            state = meta.get("retry")
            if state and state["status"] == "running":
                self.failed_attempt(alias, state, "INTERRUPTED")
                self.engine.save_meta(alias)

    def scope(self, alias: str) -> str:
        return urlsplit(str(self.engine.repos[alias]["repository"]["clone_url"])).netloc

    def cooldown(self, host: str) -> float:
        engine = self.engine
        return max(
            (
                engine.discovery_state(f"{provider.type}:{host}").get("retry_at", 0)
                for provider in engine.config.providers
                if provider.host == host
            ),
            default=0.0,
        )

    def defer_discovery(
        self, alias: str, *, cooldown: float = 0, checked: bool
    ) -> None:
        engine = self.engine
        state = engine.repos[alias]["retry"]
        count = state.get("discovery_deferrals", 0)
        reason = "PROVIDER_COOLDOWN" if cooldown else "DISCOVERY_TEMPORARY"
        due = max(state["next_attempt_at"], cooldown)
        if checked:
            if count >= MAX_DISCOVERY_DEFERRALS:
                self.block(alias, "DISCOVERY_LIMIT_REACHED")
                return
            due = max(due, self.clock() + engine.config.retry.delay(count))
            count += 1
        # Out-of-range dates cannot be represented safely in status output.
        if due > 253402300799:
            self.block(alias, "PROVIDER_COOLDOWN" if cooldown else "DISCOVERY_FAILED")
            return
        state.update(
            next_attempt_at=due, block_reason=reason, discovery_deferrals=count
        )
        engine.checkpoint("before_retry_discovery_deferral")
        engine.save_meta(alias)
        engine.checkpoint("retry_discovery_deferred")
        engine.events.log(
            "retry.cooldown_deferred" if cooldown else "retry.discovery_deferred",
            repository=alias,
            error_code=reason,
            deferrals=count,
            due_at=due,
            legacy=(
                "retry deferred repository=%s reason=%s discovery_deferrals=%d",
                alias,
                reason,
                count,
            ),
        )

    def execute(self, alias: str) -> None:
        with self.engine.events.run():
            self.execute_due(alias)

    def execute_due(self, alias: str) -> None:
        from repoharbor.recovery import recover

        engine = self.engine
        meta = engine.repos[alias]
        state = meta.get("retry")
        if (
            not engine.config.auto_retry
            or engine.git.stop.is_set()
            or not state
            or state["status"] != "pending"
            or state["next_attempt_at"] > self.clock()
        ):
            return
        if state["attempts"] >= engine.config.retry.max_retries:
            self.exhaust(alias, state)
            engine.save_meta(alias)
            return
        provider_scope = alias.rpartition(":")[0]
        if (
            provider_scope
            and (
                not any(
                    f"{p.type}:{p.host}" == provider_scope
                    for p in engine.config.providers
                )
                or {alias, *meta["repository"]["aliases"]}.intersection(
                    engine.config.selection.excluded
                )
            )
        ) or (not provider_scope and meta["selection"]["status"] == "excluded"):
            self.block(alias, "NOT_SELECTED")
            return
        safe_tree(engine.config.root)
        host = self.scope(alias)
        if "manual_attention_required" in meta["backup"]:
            self.block(alias, "RECOVERY_REQUIRED")
            return
        cooldown = self.cooldown(host)
        if cooldown > self.clock():
            self.defer_discovery(alias, cooldown=cooldown, checked=False)
            return
        folder = engine.path(alias)
        reconciled = engine.reconcile_operation(retry=True)
        if state["status"] != "pending" or engine.git.stop.is_set():
            return
        if not reconciled:
            self.block(alias, "RECOVERY_REQUIRED")
            return
        failures = engine.discover(host)
        if engine.git.stop.is_set():
            return
        cooldown = self.cooldown(host)
        if cooldown > self.clock():
            self.defer_discovery(alias, cooldown=cooldown, checked=True)
            return
        source = next((s for s in engine.sources if s.alias == alias), None)
        if provider_scope:
            failures.intersection_update({provider_scope})
        elif source is None or (failures and engine.provider_mirror_conflict(source)):
            self.block(alias, "NOT_SELECTED")
            return
        outcomes = [engine.discovery_state(scope) for scope in failures]
        if any(
            outcome.get("code") in ("GITHUB_RATE_LIMIT", "GITLAB_RATE_LIMIT")
            for outcome in outcomes
        ):
            # A zero/expired Retry-After still ends work for this check.
            self.defer_discovery(alias, cooldown=self.clock(), checked=True)
            return
        if failures and (
            source is None or engine.manual_requires_discovery(source, failures)
        ):
            if all(
                outcome.get("state") == "failed" and outcome.get("temporary") is True
                for outcome in outcomes
            ):
                self.defer_discovery(alias, checked=True)
            else:
                self.block(alias, "DISCOVERY_FAILED")
            return
        if source is None:
            self.block(alias, "NOT_SELECTED")
            return
        # Discovery may have replaced the repository metadata object.
        meta = engine.repos[alias]
        state = meta["retry"]
        if (folder / "repository.git.partial").exists() or (
            meta["backup"].get("quarantine", {}).get("state") == "pending"
        ):
            recover(engine, repository=alias, during_run=True, allow_repair=False)
        if engine.git.stop.is_set():
            return
        backup, integrity = meta["backup"], meta["integrity"]
        mirror = folder / "repository.git"
        if (
            "manual_attention_required" in backup
            or backup.get("repair_deferred")
            or integrity["status"]
            in ("suspect", "corrupt", "repairing", "repair_failed")
            or integrity.get("periodic", {}).get("repair_pending")
            or (folder / "repository.git.partial").exists()
            or backup.get("quarantine", {}).get("state") == "pending"
            or (backup["created_at"] is not None and not mirror.is_dir())
            or (mirror.exists() and integrity["status"] != "healthy")
            or any(mirror.rglob("*.lock"))
        ):
            self.block(alias, "RECOVERY_REQUIRED")
            return
        if failures and not provider_scope:
            from repoharbor.integrity import inspect

            finding, _ = inspect(engine.git, mirror)
            if engine.git.stop.is_set():
                return
            if finding != "healthy":
                self.block(alias, "RECOVERY_REQUIRED")
                return
        status = engine.refresh_storage(alias)
        if status:
            backup["status"] = status
            self.block(alias, status)
            return
        if engine.git.stop.is_set():
            return
        state.update(
            status="running",
            attempts=state["attempts"] + 1,
            last_attempt_at=self.clock(),
            last_result=None,
            last_error_code=None,
            next_attempt_at=None,
            block_reason=None,
        )
        engine.save_meta(alias)
        engine.checkpoint("retry_claimed")
        engine.events.log(
            "retry.started",
            repository=alias,
            attempts=state["attempts"],
            legacy=("retry started repository=%s attempt=%d", alias, state["attempts"]),
        )
        if not engine.backup(source) and state["status"] == "running":
            # A safety preflight can still change after the durable claim.
            code = "INTERRUPTED" if engine.git.stop.is_set() else "RECOVERY_REQUIRED"
            self.failed_attempt(alias, state, code)
            if state["status"] == "running":
                self.block(alias, code)
            else:
                engine.save_meta(alias)
