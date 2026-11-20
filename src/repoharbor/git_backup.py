"""Git subprocess boundary: arguments only, bounded, no inherited credentials/config."""

import contextlib
import os
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from repoharbor.credentials import Credentials
from repoharbor.errors import GitError, HarborError


class Git:
    def __init__(
        self,
        credentials: Credentials,
        stop: threading.Event,
        timeout: float = 3600,
        *,
        extra_fds: tuple[int, ...] = (),
        output_errors: str = "replace",
        scratch: Path | None = None,
    ):
        self.credentials, self.stop, self.timeout = credentials, stop, timeout
        self.remote_head: str | None = None
        self.backup_lock_fd: int | None = None
        self.extra_fds = extra_fds
        self.output_errors = output_errors
        self.scratch = scratch

    def run(
        self,
        args: list[str],
        credential: str | None = None,
        interrupt: Callable[[], bool] = lambda: False,
        *,
        probe: Literal["config", "structure", "fsck"] | None = None,
    ) -> str:
        if self.stop.is_set():
            raise HarborError("INTERRUPTED")
        env = self.credentials.git_env(credential if probe is None else None)
        if probe is not None:
            env.update(
                LC_ALL="C",
                GIT_ALLOW_PROTOCOL="",
                GIT_NO_LAZY_FETCH="1",
                GIT_NO_REPLACE_OBJECTS="1",
                GIT_OPTIONAL_LOCKS="0",
                GIT_ASKPASS="/bin/false",
            )
        with contextlib.ExitStack() as stack:
            # Periodic probes drain a bounded pipe without writing temporary output.
            out = (
                stack.enter_context(tempfile.TemporaryFile(dir=self.scratch))
                if probe is None
                else None
            )
            err = (
                stack.enter_context(tempfile.TemporaryFile(dir=self.scratch))
                if probe is None
                else None
            )
            process = subprocess.Popen(
                [shutil.which("git") or "/usr/bin/git", *args],
                env=env,
                stdout=subprocess.PIPE if probe is not None else out,
                stderr=subprocess.STDOUT if probe is not None else err,
                start_new_session=True,
                # Keep the root locked while Git survives a daemon SIGKILL.
                pass_fds=self.extra_fds
                + (() if self.backup_lock_fd is None else (self.backup_lock_fd,)),
            )
            started = time.monotonic()
            try:
                if probe is not None:
                    assert process.stdout is not None
                    stack.enter_context(process.stdout)
                    return self.probe_output(process, started, probe == "fsck")
                assert out is not None and err is not None
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
                return out.read().decode("utf-8", errors=self.output_errors)
            finally:
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def probe_output(
        self, process: subprocess.Popen[bytes], started: float, fsck: bool
    ) -> str:
        assert process.stdout is not None
        fd = process.stdout.fileno()
        output = bytearray()
        tail = b""
        filesystem_failure = False
        eof = False
        while not eof or process.poll() is None:
            if self.stop.is_set():
                raise GitError("INTERRUPTED")
            if time.monotonic() - started > self.timeout:
                raise GitError("GIT_TIMEOUT")
            if eof:
                self.stop.wait(0.05)
                continue
            readable, _, _ = select.select([fd], [], [], 0.05)
            if not readable:
                continue
            chunk = os.read(fd, 8192)
            eof = not chunk
            diagnostic = tail + chunk.lower()
            filesystem_failure |= any(
                marker in diagnostic
                for marker in (
                    b"permission denied",
                    b"operation not permitted",
                    b"input/output error",
                    b"no space left on device",
                    b"stale file handle",
                    b"too many open files",
                    b"cannot allocate memory",
                )
            )
            tail = diagnostic[-64:]
            if not fsck:
                output.extend(chunk)
                if len(output) > 65536:
                    raise GitError("GIT_OUTPUT_LIMIT")
        if filesystem_failure:
            raise GitError("FILESYSTEM_ERROR")
        if process.returncode:
            raise GitError(
                "GIT_FAILED" if process.returncode > 0 else "GIT_INTERRUPTED"
            )
        return output.decode("utf-8", errors="replace")

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
