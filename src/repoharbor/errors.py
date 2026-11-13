class HarborError(Exception):
    """A safe, public error message (never raw Git/YAML/OS diagnostics)."""


class GitError(HarborError):
    """A fixed code and optional evidence about the source; never raw diagnostics."""

    def __init__(self, code: str, source_status: str | None = None):
        super().__init__(code)
        self.source_status = source_status
