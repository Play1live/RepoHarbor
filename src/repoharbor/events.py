"""Structured, bounded service events with no free-form external diagnostics."""

import json
import logging
import re
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Lock
from typing import TextIO
from uuid import uuid4

from repoharbor.config import Config
from repoharbor.log_files import RotatingLog

Field = str | int | float | bool | None
RUN_ID: ContextVar[str | None] = ContextVar("run_id", default=None)

# Only callers supplying these fields can put data in an event. All text fields
# are identifiers or fixed result/error codes, never messages, paths or URLs.
FIELDS = frozenset(
    {
        "repository",
        "provider",
        "authority",
        "operation_id",
        "trigger",
        "kind",
        "result",
        "error_code",
        "temporary",
        "attempts",
        "due_at",
        "deferrals",
        "detected",
        "considered",
        "excluded",
        "checked",
        "damaged",
        "skipped",
    }
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,286}\Z")


# Provider transport codes are shared with GitLab's HTTPS client.
TRANSPORT_CODES = (
    "DISCOVERY_TIMEOUT",
    "FORBIDDEN",
    "HTTP_ERROR",
    "INVALID_RESPONSE_OR_NETWORK",
    "NOT_ACCESSIBLE",
    "RATE_LIMIT",
    "RESPONSE_TOO_LARGE",
    "UNAUTHORIZED",
)
ERROR_CODES = frozenset(
    {
        *(
            f"{provider}_{code}"
            for provider in ("GITHUB", "GITLAB")
            for code in TRANSPORT_CODES
        ),
        "GITHUB_ALIAS_CONFLICT",
        "GITLAB_ALIAS_CONFLICT",
        "GITHUB_INCONSISTENT_ID",
        "GITHUB_INVALID_PAGE",
        "GITHUB_INVALID_PAGINATION",
        "GITHUB_INVALID_REPOSITORY",
        "GITHUB_INVALID_USER",
        "GITHUB_PAGINATION_LIMIT",
        "GITLAB_INCOMPLETE_PAGINATION",
        "GITLAB_INCONSISTENT_ID",
        "GITLAB_INCONSISTENT_PAGINATION",
        "GITLAB_INVALID_ENDPOINT",
        "GITLAB_INVALID_GROUP",
        "GITLAB_INVALID_ID",
        "GITLAB_INVALID_PAGE",
        "GITLAB_INVALID_PAGINATION",
        "GITLAB_INVALID_PROJECT",
        "GITLAB_PAGINATION_LIMIT",
        "GITLAB_UNBOUND_INSTANCE",
        "GITLAB_UNSUPPORTED_USER",
        "GIT_CORRUPTION",
        "GIT_FAILED",
        "GIT_INTERRUPTED",
        "GIT_NETWORK",
        "GIT_OUTPUT_LIMIT",
        "GIT_INVALID_REFS",
        "GIT_SNAPSHOT_CHANGED",
        "GIT_SERVER_TEMPORARY",
        "GIT_TIMEOUT",
        "BACKUP_FAILED",
        "DISCOVERY_FAILED",
        "DISCOVERY_LIMIT_REACHED",
        "DISCOVERY_REQUIRED",
        "DISCOVERY_TEMPORARY",
        "EACCES",
        "EIO",
        "ENOSPC",
        "EXCLUDED_PARTIAL_PRESERVED",
        "FILESYSTEM_ERROR",
        "FREE_SPACE_RESERVE",
        "INTERRUPTED",
        "INVALID_MIRROR",
        "MIRROR_MISSING",
        "MIRROR_UNSUPPORTED",
        "NOT_SELECTED",
        "PARTIAL_UNOWNED_OR_QUARANTINE_FULL",
        "PROVIDER_COOLDOWN",
        "PROVIDER_IDENTITY_CONFLICT",
        "QUARANTINE_CONFLICT",
        "QUARANTINE_CONTENT_MISSING",
        "RECOVERY_REQUIRED",
        "REPAIR_FAILED",
        "LOG_MOUNT_MISSING",
        "LOG_MOUNT_UNSAFE",
        "LOG_FILE_UNSAFE",
        "LOG_FILE_UNAVAILABLE",
        "global_limit_paused",
        "repo_limit_reached",
    }
)


class EventFormat(logging.Formatter):
    def __init__(self, output: str):
        super().__init__()
        self.output = output

    def format(self, record: logging.LogRecord) -> str:
        stamp = (
            datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        event = {
            "timestamp": stamp,
            "level": record.levelname,
            **vars(record)["fields"],
        }
        if self.output == "json":
            return json.dumps(event, separators=(",", ":"), allow_nan=False)
        return " ".join(f"{key}={value}" for key, value in event.items())


class Console(logging.StreamHandler[TextIO]):
    def handleError(self, record: logging.LogRecord) -> None:
        # Logging's default error handler prints the record and a traceback.
        pass


class Events:
    def __init__(self, config: Config | None = None):
        self.logger: logging.Logger | None = None
        self.file: RotatingLog | None = None
        self.recent: deque[dict[str, Field]] | None = (
            deque(maxlen=250) if config is not None and config.web.enabled else None
        )
        self.recent_lock = Lock()
        if config is not None and config.extended_logging:
            self.logger = logging.Logger("repoharbor.events", config.logging.level)
            self.logger.propagate = False
            self.console = Console()
            self.console.setFormatter(EventFormat(config.logging.format))
            self.logger.addHandler(self.console)

    def open_file(self, config: Config) -> None:
        if (
            self.logger is not None
            and config.logging.file.enabled
            and self.file is None
        ):
            self.file = RotatingLog(config, self.file_failed)
            self.file.setFormatter(self.console.formatter)
            self.logger.addHandler(self.file)

    def file_failed(self) -> None:
        # Bypass the file handler and keep this diagnostic visible at every level.
        record = logging.LogRecord(
            "repoharbor.events", logging.ERROR, "", 0, "", (), None
        )
        vars(record)["fields"] = {
            "event": "logging.file_failed",
            "error_code": "LOG_WRITE_FAILED",
        }
        self.console.handle(record)

    def close(self) -> None:
        if self.logger is not None:
            for handler in self.logger.handlers[:]:
                self.logger.removeHandler(handler)
                with suppress(OSError, ValueError):
                    handler.flush()
                handler.close()

    def log(
        self,
        event: str,
        level: int = logging.INFO,
        *,
        legacy: tuple[object, ...] = (),
        started: float | None = None,
        **fields: Field,
    ) -> None:
        if self.logger is None and legacy:
            message, *args = legacy
            logging.log(level, str(message), *args)
        should_log = self.logger is not None and self.logger.isEnabledFor(level)
        if not should_log and self.recent is None:
            return
        safe: dict[str, Field] = {"event": event}
        for key, value in fields.items():
            if key not in FIELDS or value is None:
                continue
            if key == "error_code":
                safe[key] = (
                    value
                    if isinstance(value, str) and value in ERROR_CODES
                    else "OPERATION_FAILED"
                )
            elif (isinstance(value, str) and IDENTIFIER.fullmatch(value)) or (
                isinstance(value, (int, float)) and -(2**63) <= value <= 2**63 - 1
            ):
                safe[key] = value
        if (run_id := RUN_ID.get()) is not None:
            safe["run_id"] = run_id
        if started is not None:
            safe["duration_ms"] = max(0, int((time.monotonic() - started) * 1000))
        if self.recent is not None:
            with self.recent_lock:
                self.recent.append({"timestamp": datetime.now(UTC).isoformat(), **safe})
        # Logging must never turn an operation's successful mutation into a failure.
        if self.logger is not None and should_log:
            with suppress(Exception):
                self.logger.log(level, event, extra={"fields": safe})

    def recent_events(self) -> list[dict[str, Field]]:
        with self.recent_lock:
            return list(self.recent or ())

    def start(self) -> float | None:
        return (
            time.monotonic()
            if self.logger is not None or self.recent is not None
            else None
        )

    @contextmanager
    def run(self) -> Iterator[float | None]:
        if self.logger is None and self.recent is None:
            yield None
            return
        token = RUN_ID.set(uuid4().hex)
        try:
            yield self.start()
        finally:
            RUN_ID.reset(token)
