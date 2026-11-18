from enum import IntEnum


class HarborError(Exception):
    """A safe, public error message (never raw Git/YAML/OS diagnostics)."""


class GitError(HarborError):
    """A fixed code and optional evidence about the source; never raw diagnostics."""

    def __init__(self, code: str, source_status: str | None = None):
        super().__init__(code)
        self.source_status = source_status


class Result(IntEnum):
    SUCCESS = 0
    INVALID_ARGUMENT = 2
    UNKNOWN_REPOSITORY = 3
    FEATURE_DISABLED = 4
    SERVICE_UNREACHABLE = 5
    BUSY = 6
    NOT_SELECTED = 7
    DISCOVERY_FAILED = 8
    BACKUP_FAILED = 9
    STATE_UNAVAILABLE = 10
