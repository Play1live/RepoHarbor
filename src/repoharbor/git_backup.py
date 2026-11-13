"""Git subprocess boundary: arguments only, bounded, no inherited credentials/config."""

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from repoharbor.credentials import Credentials
from repoharbor.errors import GitError, HarborError


class Git:
    def __init__(
        self, credentials: Credentials, stop: threading.Event, timeout: float = 3600
    ):
        self.credentials, self.stop, self.timeout = credentials, stop, timeout
        self.remote_head: str | None = None

    def run(
        self,
        args: list[str],
        credential: str | None = None,
        interrupt: Callable[[], bool] = lambda: False,
    ) -> str:
        if self.stop.is_set():
            raise HarborError("INTERRUPTED")
        env = self.credentials.git_env(credential)
        # File output avoids pipe deadlocks and unlimited RAM for Git diagnostics.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(
                [shutil.which("git") or "/usr/bin/git", *args],
                env=env,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if (
                        self.stop.is_set()
                        or interrupt()
                        or time.monotonic() - started > self.timeout
                    ):
                        raise HarborError("INTERRUPTED")
                    self.stop.wait(0.1)
                if process.returncode:
                    # Classify bounded diagnostics; never persist/print their contents.
                    err.seek(0)
                    diagnostic = err.read(65536).lower()
                    if b"no space left on device" in diagnostic:
                        raise GitError("ENOSPC")
                    remote = any(
                        command in args for command in ("clone", "fetch", "ls-remote")
                    )
                    unavailable = remote and any(
                        marker in diagnostic
                        for marker in (
                            b"could not resolve host",
                            b"failed to connect",
                            b"unable to access",
                            b"authentication failed",
                            b"could not read from remote",
                            b"does not appear to be a git repository",
                            b"repository not found",
                        )
                    )
                    raise GitError("GIT_FAILED", "unavailable" if unavailable else None)
                out.seek(0)
                return out.read().decode("utf-8", errors="replace")
            finally:
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def refs(self, mirror: Path) -> str:
        return self.run(
            ["-C", str(mirror), "for-each-ref", "--format=%(objectname) %(refname)"]
        )

    def remote_refs(self, url: str, credential: str | None) -> str:
        raw = self.run(["ls-remote", "--symref", "--", url], credential)
        self.remote_head = None
        refs = []
        for line in raw.splitlines():
            fields = line.split()
            if fields[0] == "ref:" and fields[-1] == "HEAD":
                self.remote_head = fields[1]
            elif fields[-1] == "HEAD" and self.remote_head is None:
                self.remote_head = fields[0]
            elif fields[-1].startswith("refs/") and not fields[-1].endswith("^{}"):
                refs.append(" ".join(fields))
        return "\n".join(sorted(refs))

    def head(self, mirror: Path) -> str | None:
        """Return the symbolic target or the object ID of a detached HEAD."""
        try:
            return self.run(
                ["-C", str(mirror), "symbolic-ref", "--quiet", "HEAD"]
            ).strip()
        except HarborError:
            try:
                return self.run(
                    ["-C", str(mirror), "rev-parse", "--verify", "HEAD"]
                ).strip()
            except HarborError:
                return None

    def healthy(self, mirror: Path) -> bool:
        try:
            if (
                self.run(
                    ["-C", str(mirror), "rev-parse", "--is-bare-repository"]
                ).strip()
                != "true"
            ):
                return False
            self.run(["-C", str(mirror), "fsck", "--full"], interrupt=lambda: False)
        except HarborError:
            if self.stop.is_set():
                raise
            return False
        return True

    def clone(
        self,
        url: str,
        partial: Path,
        credential: str | None,
        interrupt: Callable[[], bool],
    ) -> None:
        self.run(["clone", "--mirror", "--", url, str(partial)], credential, interrupt)
        if not self.healthy(partial):
            raise HarborError("INVALID_MIRROR")

    def fetch(
        self,
        mirror: Path,
        url: str,
        credential: str | None,
        interrupt: Callable[[], bool],
        repair: bool = False,
    ) -> None:
        # Explicit URL/refspec defeats altered remote URLs, helpers and push settings.
        args = ["-C", str(mirror), "fetch", "--atomic", "--no-recurse-submodules"]
        args += (
            ["--refetch", "--no-write-fetch-head", "--refmap="]
            if repair
            else ["--prune"]
        )
        # Explicit object-ID source refspecs have no destination; --refmap= disables
        # any implicit remote mapping. Existing refs are never rewritten by repair.
        refs = (
            sorted({line.split()[0] for line in self.refs(mirror).splitlines()})
            if repair
            else ["+refs/*:refs/*"]
        )
        head = self.head(mirror) if repair else self.remote_head
        if head and not head.startswith("refs/"):
            refs.append(head)
        args += ["--", url, *refs]
        self.run(args, credential, interrupt)
        if not repair and self.remote_head:
            if self.remote_head.startswith("refs/"):
                self.run(["-C", str(mirror), "symbolic-ref", "HEAD", self.remote_head])
            else:
                self.run(
                    [
                        "-C",
                        str(mirror),
                        "update-ref",
                        "--no-deref",
                        "HEAD",
                        self.remote_head,
                    ]
                )
