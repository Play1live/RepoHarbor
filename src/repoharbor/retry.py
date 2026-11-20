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

BLOCK_REASONS = (
    "BACKUP_FAILED",
    "DISCOVERY_FAILED",
    "PROVIDER_COOLDOWN",
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
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError
    status, attempts = value["status"], value["attempts"]
    if (
        status not in ("pending", "running", "succeeded", "blocked", "exhausted")
        or type(attempts) is not int
        or not 0 <= attempts <= MAX_RETRIES
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
            and value["block_reason"] not in (None, "PROVIDER_COOLDOWN")
        )
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
    return {
        "status": status,
        "pending": enabled and status == "pending" and not exhausted,
        "attempts": attempts,
        "max_retries": maximum,
        "next_attempt_at": iso(state.get("next_attempt_at")),
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
                state.update(last_result="succeeded", last_error_code=None)
                logging.info("retry succeeded repository=%s", alias)
            state.update(
                status="succeeded", attempts=0, next_attempt_at=None, block_reason=None
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
        if state.get("status") == "exhausted":
            return None
        state.update(reason=code, block_reason=None)
        if state["attempts"] >= engine.config.retry.max_retries:
            self.exhaust(alias, state)
        else:
            # Another scheduled or manual failure must not postpone an existing retry.
            due = state.get("next_attempt_at") or (
                max(self.clock(), state["last_attempt_at"] or 0)
                + engine.config.retry.delay(state["attempts"])
            )
            state.update(status="pending", next_attempt_at=due)
            logging.info(
                "retry scheduled repository=%s reason=%s attempts=%d due=%s",
                alias,
                code,
                state["attempts"],
                due,
            )
        return str(state["status"])

    def failed_attempt(self, alias: str, state: dict[str, Any], code: str) -> None:
        state.update(
            last_result="interrupted" if code == "INTERRUPTED" else "failed",
            last_error_code=code,
        )
        logging.warning("retry failed repository=%s reason=%s", alias, code)
        if code == "INTERRUPTED":
            self.block(alias, code, save=False)

    def block(self, alias: str, reason: str, *, save: bool = True) -> None:
        engine = self.engine
        state = engine.repos[alias].get("retry")
        if state and state["status"] in ("pending", "running"):
            state.update(status="blocked", next_attempt_at=None, block_reason=reason)
            if save:
                engine.save_meta(alias)
            logging.warning("retry blocked repository=%s reason=%s", alias, reason)

    def exhaust(self, alias: str, state: dict[str, Any]) -> None:
        state.update(status="exhausted", next_attempt_at=None, block_reason=None)
        logging.warning(
            "retry limit reached repository=%s attempts=%d", alias, state["attempts"]
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
        data = engine.store.data
        return max(
            (
                (
                    data.get("discovery", {})
                    if provider.type == "github"
                    else data.get("provider_discovery", {}).get(f"gitlab:{host}", {})
                ).get("retry_at", 0)
                for provider in engine.config.providers
                if provider.host == host
            ),
            default=0.0,
        )

    def defer_cooldown(self, alias: str, until: float) -> None:
        state = self.engine.repos[alias]["retry"]
        # Out-of-range server dates cannot be represented safely in status output.
        if until > 253402300799:
            self.block(alias, "PROVIDER_COOLDOWN")
            return
        state.update(
            next_attempt_at=max(state["next_attempt_at"], until),
            block_reason="PROVIDER_COOLDOWN",
        )
        self.engine.save_meta(alias)
        logging.info("retry deferred repository=%s reason=PROVIDER_COOLDOWN", alias)

    def execute(self, alias: str) -> None:
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
        if meta["selection"]["status"] == "excluded" and not any(
            f"{p.type}:{p.host}" == alias.rpartition(":")[0]
            for p in engine.config.providers
        ):
            self.block(alias, "NOT_SELECTED")
            return
        safe_tree(engine.config.root)
        host = self.scope(alias)
        cooldown = self.cooldown(host)
        if cooldown > self.clock():
            self.defer_cooldown(alias, cooldown)
            return
        folder = engine.path(alias)
        if "manual_attention_required" in meta[
            "backup"
        ] or not engine.reconcile_operation(retry=True):
            self.block(alias, "RECOVERY_REQUIRED")
            return
        if state["status"] != "pending" or engine.git.stop.is_set():
            return
        failures = engine.discover(host)
        if engine.git.stop.is_set():
            return
        cooldown = self.cooldown(host)
        if cooldown > self.clock():
            self.defer_cooldown(alias, cooldown)
            return
        if failures:
            for key in engine.repos:
                if self.scope(key) == host:
                    self.block(key, "DISCOVERY_FAILED")
            return
        source = next((s for s in engine.sources if s.alias == alias), None)
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
        logging.info("retry started repository=%s attempt=%d", alias, state["attempts"])
        if not engine.backup(source) and state["status"] == "running":
            # A safety preflight can still change after the durable claim.
            code = "INTERRUPTED" if engine.git.stop.is_set() else "RECOVERY_REQUIRED"
            self.failed_attempt(alias, state, code)
            if state["status"] == "running":
                self.block(alias, code)
            else:
                engine.save_meta(alias)
