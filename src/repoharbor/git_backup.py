"""Git subprocess boundary: arguments only, bounded, no inherited credentials/config."""

import contextlib
import os
import re
import select
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from repoharbor.credentials import Credentials
from repoharbor.errors import GitError, HarborError

GIT_OUTPUT_MAX = 32 * 1024 * 1024
DIAGNOSTIC_MAX = 65536


def transient_git_error(diagnostic: bytes) -> str | None:
    # Only recognize Git/libcurl messages. Server-supplied "remote:" text,
    # generic transfer errors, and permanent HTTP responses are insufficient.
    access = re.findall(
        rb"^fatal: unable to access '[^\r\n]*?': ([^\r\n]*)$", diagnostic, re.M
    )
    http = rb"the requested url returned error: ([0-9]{3})$"
    statuses = re.findall(
        rb"^error: rpc failed; http ([0-9]{3}) curl 22 ", diagnostic, re.M
    )
    statuses += [m[1] for line in access if (m := re.fullmatch(http, line))]
    if statuses:
        if all(code in (b"500", b"502", b"503", b"504") for code in statuses):
            return "GIT_SERVER_TEMPORARY"
        return None
    if any(
        re.match(rb"(?:failed to connect to |recv failure: |send failure: )", line)
        for line in access
    ) or re.search(rb"^error: rpc failed; curl (18|52|55|56) ", diagnostic, re.M):
        return "GIT_NETWORK"
    if any(
        re.match(rb"(?:operation timed out|connection timed out)", line)
        for line in access
    ) or re.search(rb"^error: rpc failed; curl 28 ", diagnostic, re.M):
        return "GIT_TIMEOUT"
    return None


class Git:
    def __init__(
        self,
        credentials: Credentials,
        stop: threading.Event,
        timeout: float = 3600,
        *,
        extra_fds: tuple[int, ...] = (),
        output_errors: str = "surrogateescape",
    ):
        self.credentials, self.stop, self.timeout = credentials, stop, timeout
        self.remote_head: str | None = None
        self.backup_lock_fd: int | None = None
        self.extra_fds = extra_fds
        self.output_errors = output_errors
        self.classify_transient = False

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
        command = next(iter(args[2:] if args[:1] == ["-C"] else args), None)
        remote = args.index("--") + 1 if "--" in args else len(args)
        if credential is not None and probe is None:
            if remote >= len(args) or not args[remote].startswith("https://"):
                raise GitError("GIT_FAILED")
            env["REPOHARBOR_ASKPASS_AUTHORITY"] = urlsplit(args[remote]).netloc
            env["LC_ALL"] = "C"
        classify = (
            self.classify_transient
            and probe is None
            and command in ("clone", "fetch", "ls-remote")
            and "--refetch" not in args
            and remote < len(args)
            and args[remote].startswith("https://")
        )
        if classify:
            # RepoHarbor owns retry scheduling. Git 2.56.1 can enter its 429
            # loop after a 401 challenge even with maxRetries=0. Reject its
            # retry delay as well, including a zero or absent Retry-After.
            env.update(
                LC_ALL="C", GIT_HTTP_MAX_RETRIES="0", GIT_HTTP_MAX_RETRY_TIME="-1"
            )
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
            process = subprocess.Popen(
                [shutil.which("git") or "/usr/bin/git", *args],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if probe is not None else subprocess.PIPE,
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
                assert process.stdout is not None and process.stderr is not None
                stack.enter_context(process.stdout)
                stack.enter_context(process.stderr)
                output, diagnostic = self.command_output(
                    process,
                    started,
                    interrupt,
                    classify,
                    command not in ("clone", "fetch"),
                )
                if process.returncode:
                    diagnostic = diagnostic.lower()
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
                    code = transient_git_error(bytes(diagnostic)) if classify else None
                    raise GitError(
                        code or "GIT_FAILED", "unavailable" if unavailable else None
                    )
                return output.decode("utf-8", errors=self.output_errors)
            finally:
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def command_output(
        self,
        process: subprocess.Popen[bytes],
        started: float,
        interrupt: Callable[[], bool],
        classify: bool,
        keep_stdout: bool,
    ) -> tuple[bytearray, bytearray]:
        output, diagnostic = bytearray(), bytearray()
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            while selector.get_map() or process.poll() is None:
                if self.stop.is_set() or interrupt():
                    raise HarborError("INTERRUPTED")
                if time.monotonic() - started > self.timeout:
                    if classify:
                        raise GitError("GIT_TIMEOUT")
                    raise HarborError("INTERRUPTED")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == "err":
                        diagnostic.extend(
                            chunk[: max(0, DIAGNOSTIC_MAX - len(diagnostic))]
                        )
                    elif keep_stdout:
                        output.extend(chunk)
                        if len(output) > GIT_OUTPUT_MAX:
                            raise GitError("GIT_OUTPUT_LIMIT")
        return output, diagnostic

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

    def require_managed_config(self, mirror: Path) -> None:
        flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDONLY
        # Parse Git's syntax with includes disabled. Every setting used when opening
        # the existing mirror must belong to the configuration of a managed clone.
        try:
            with os.fdopen(os.open(mirror / "config", flags), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise GitError("MIRROR_UNSUPPORTED")
        except OSError:
            raise GitError("FILESYSTEM_ERROR") from None
        approved = {
            "core.repositoryformatversion",
            "core.filemode",
            "core.bare",
            "core.logallrefupdates",
            "core.ignorecase",
            "core.precomposeunicode",
            "core.symlinks",
            "extensions.objectformat",
            "extensions.refstorage",
            "remote.origin.url",
            "remote.origin.fetch",
            "remote.origin.tagopt",
            "remote.origin.mirror",
        }
        keys = self.run(
            [
                "config",
                "--file",
                str(mirror / "config"),
                "--no-includes",
                "--null",
                "--name-only",
                "--list",
            ],
            probe="config",
        )
        if any(key not in approved for key in keys.lower().split("\0") if key):
            raise GitError("MIRROR_UNSUPPORTED")
        if any(
            (mirror / item).exists()
            for item in (
                "commondir",
                "config.worktree",
                "shallow",
                "objects/info/alternates",
                "objects/info/http-alternates",
                "info/grafts",
            )
        ) or any((mirror / "objects/pack").glob("*.promisor")):
            raise GitError("MIRROR_UNSUPPORTED")

    def restore_compatibility(self, mirror: Path) -> str:
        try:
            self.require_managed_config(mirror)
            entries = (
                self.run(
                    [
                        "config",
                        "--file",
                        str(mirror / "config"),
                        "--no-includes",
                        "--null",
                        "--list",
                    ],
                    probe="config",
                )
                .lower()
                .split("\0")
            )
            if any(
                entry.startswith("extensions.refstorage\n")
                and entry != "extensions.refstorage\nfiles"
                for entry in entries
            ):
                return "unsupported"
            head = self.head(mirror)
            if self.refs(mirror) or (head and not head.startswith("refs/")):
                return "clone_and_bundle"
            return "clone_only"
        except (OSError, HarborError):
            return "unconfirmed"

    def refs(self, mirror: Path) -> str:
        return self.run(
            ["-C", str(mirror), "for-each-ref", "--format=%(objectname) %(refname)"]
        ).removesuffix("\n")

    def remote_refs(self, url: str, credential: str | None) -> str:
        raw = self.run(["ls-remote", "--symref", "--", url], credential)
        self.remote_head = None
        refs = []
        # Git ref names can contain UTF-8 forms of whitespace but cannot contain TAB/LF.
        for line in raw.split("\n"):
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 2:
                raise GitError("GIT_INVALID_REFS")
            if fields[0].startswith("ref: "):
                if fields[1] == "HEAD":
                    self.remote_head = fields[0][5:]
                continue
            elif not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", fields[0]):
                raise GitError("GIT_INVALID_REFS")
            if fields[-1] == "HEAD" and self.remote_head is None:
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
        self.require_managed_config(mirror)
        # Explicit URL/refspec avoids using configured remote URLs and mappings.
        args = [
            "-C",
            str(mirror),
            "fetch",
            "--atomic",
            "--no-auto-gc",
            "--no-recurse-submodules",
        ]
        args += (
            ["--refetch", "--no-write-fetch-head", "--refmap="]
            if repair
            else ["--prune", "--refmap="]
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
