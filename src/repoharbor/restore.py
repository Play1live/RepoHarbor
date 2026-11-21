"""Read-only, local restore from the persisted mirror inventory."""

import ctypes
import errno
import fcntl
import logging
import os
import shutil
import signal
import stat
import tempfile
import threading
from argparse import Namespace
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from repoharbor.catalog import directory, read_metadata
from repoharbor.config import Config
from repoharbor.credentials import Credentials
from repoharbor.errors import GitError, HarborError, Result
from repoharbor.events import Events
from repoharbor.git_backup import Git
from repoharbor.log_files import LogSetupError
from repoharbor.mounts import aliases_protected_mount as aliases_protected_mount
from repoharbor.persistence import (
    directory_fd,
    read_json,
    safe_tree,
    sync_tree,
    validate_state,
)

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
AT_FDCWD = -100
RENAME_NOREPLACE = 1


class RestoreError(HarborError):
    def __init__(self, result: Result, message: str):
        super().__init__(message)
        self.result = result


@contextmanager
def locked_root(root: Path) -> Iterator[int]:
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise HarborError("Restore requires an existing, safe backup root")
    try:
        with directory_fd(root) as root_fd:
            fd = os.open(
                ".repoharbor.lock",
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
    except FileNotFoundError:
        raise HarborError("Existing backup lock is missing") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise HarborError("Invalid backup lock") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HarborError("Invalid backup lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RestoreError(
                Result.BUSY, "Backup root is locked by another process"
            ) from None
        yield fd
    finally:
        os.close(fd)


def mirror_path(root: Path, repository: str) -> Path:
    state = read_json(root / "data.json")
    validate_state(state)
    operation = state["active_operation"]
    if (
        operation
        and operation["alias"] == repository
        and operation["state"]
        in (
            "running",
            "interrupted",
        )
    ):
        raise HarborError("Mirror has an unfinished backup operation")
    folder = directory(root, repository)
    if any(p.is_symlink() for p in (folder, *folder.parents)):
        raise HarborError("Unsafe mirror directory")
    if not folder.exists():
        raise RestoreError(
            Result.UNKNOWN_REPOSITORY, "Repository is absent from local inventory"
        )
    meta = read_metadata(repository, folder)
    backup = meta["backup"]
    if backup["relative_path"] != "repository.git":
        raise HarborError("Mirror path disagrees with repository metadata")
    if (
        "partial_operation" in backup
        or backup.get("quarantine", {}).get("state") == "pending"
        or (folder / "repository.git.partial").exists()
        or (folder / "repository.git.partial").is_symlink()
    ):
        raise HarborError("Mirror has an unfinished backup operation")
    mirror = folder / "repository.git"
    if not mirror.is_dir():
        raise HarborError("Local mirror is missing")
    safe_tree(mirror)
    if (
        any(
            (mirror / name).exists()
            for name in (
                "shallow",
                "objects/info/alternates",
                "objects/info/http-alternates",
                "info/grafts",
            )
        )
        or any((mirror / "objects/pack").glob("*.promisor"))
        or any(mirror.rglob("*.lock"))
    ):
        raise HarborError(
            "Mirror is incomplete, externally linked or has Git lock files"
        )
    return mirror


@contextmanager
def destination(config: Config, value: str) -> Iterator[tuple[int, Path]]:
    path = Path(value).absolute()
    path = Path("/", *path.parts[1:])
    protected = (config.root.resolve(), config.secrets_dir.resolve())
    if (
        not value
        or not path.name
        or ".." in path.parts
        or any(path.is_relative_to(p) for p in protected)
        or path.is_relative_to(config.secrets_dir)
    ):
        raise RestoreError(
            Result.INVALID_ARGUMENT, "Invalid or protected restore destination"
        )
    with ExitStack() as stack:
        fd = os.open("/", DIRECTORY_FLAGS)
        stack.callback(os.close, fd)
        protected_inodes = [p.stat() for p in protected if p.exists()]
        for component in path.parts[1:-1]:
            fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            stack.callback(os.close, fd)
            entry = os.fstat(fd)
            if any(os.path.samestat(entry, item) for item in protected_inodes):
                raise RestoreError(
                    Result.INVALID_ARGUMENT,
                    "Restore destination is under a protected mount",
                )
        if aliases_protected_mount(
            path.parent, protected, Path("/proc/self/mountinfo")
        ):
            raise RestoreError(
                Result.INVALID_ARGUMENT,
                "Restore destination is under a protected mount",
            )
        try:
            os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            yield fd, path
        else:
            raise RestoreError(
                Result.DESTINATION_FAILED, "Restore destination already exists"
            )


def publish(source: Path, parent_fd: int, name: str) -> None:
    # Linux renameat2 works for files and directories and refuses to replace even
    # an empty directory. A filesystem without RENAME_NOREPLACE fails closed.
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(
        AT_FDCWD, os.fsencode(source), parent_fd, os.fsencode(name), RENAME_NOREPLACE
    ):
        raise OSError(ctypes.get_errno(), "Atomic restore publication failed")


def git_at(git: Git, path: Path, *args: str) -> str:
    return git.run(
        [
            "--no-replace-objects",
            "-c",
            "protocol.https.allow=never",
            "-C",
            str(path),
            *args,
        ]
    )


def init_repo(git: Git, path: Path, object_format: str, *, bare: bool) -> None:
    args = [
        "init",
        "--quiet",
        "--template=",
        "--initial-branch=main",
        f"--object-format={object_format}",
    ]
    if bare:
        args.append("--bare")
    git_at(git, path.parent, *args, str(path))


def source_view(git: Git, mirror: Path, stage: Path) -> tuple[Path, str]:
    # Read only structural settings. Git never loads the mirror's local config,
    # includes, hooks, replace refs or promisor settings during these operations.
    settings = git_at(
        git,
        stage,
        "config",
        "--no-includes",
        "--file",
        str(mirror / "config"),
        "--null",
        "--list",
    ).split("\0")
    bare = [entry for entry in settings if entry.startswith("core.bare\n")]
    formats = [
        entry.removeprefix("extensions.objectformat\n")
        for entry in settings
        if entry.startswith("extensions.objectformat\n")
    ]
    versions = [
        entry.removeprefix("core.repositoryformatversion\n")
        for entry in settings
        if entry.startswith("core.repositoryformatversion\n")
    ]
    extensions = {
        entry.partition("\n")[0]
        for entry in settings
        if entry.startswith("extensions.")
    }
    if (
        bare != ["core.bare\ntrue"]
        or formats not in ([], ["sha1"], ["sha256"])
        or versions not in ([], ["0"], ["1"])
        or (formats and versions != ["1"])
        or extensions - {"extensions.objectformat", "extensions.refstorage"}
        or any(
            entry.startswith("extensions.refstorage\n")
            and entry != "extensions.refstorage\nfiles"
            for entry in settings
        )
    ):
        raise HarborError("Unsupported or invalid bare mirror")
    if not (mirror / "HEAD").is_file() or not (mirror / "objects").is_dir():
        raise HarborError("Invalid bare mirror")
    object_format = formats[0] if formats else "sha1"
    view = stage / "source.git"
    init_repo(git, view, object_format, bare=True)
    # Copy HEAD as Git expects a regular file for unborn and detached heads.
    (view / "HEAD").write_bytes((mirror / "HEAD").read_bytes())
    (view / "objects" / "info").rmdir()
    (view / "objects" / "pack").rmdir()
    (view / "objects").rmdir()
    (view / "objects").symlink_to(mirror / "objects")
    if (mirror / "refs").exists():
        shutil.copytree(mirror / "refs", view / "refs", dirs_exist_ok=True)
    if (mirror / "packed-refs").exists():
        shutil.copyfile(mirror / "packed-refs", view / "packed-refs")
    git_at(git, view, "fsck", "--full", "--no-dangling")
    return view, object_format


def references(git: Git, repo: Path) -> dict[str, tuple[str, str]]:
    return {
        name: (oid, symref)
        for line in git_at(
            git, repo, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)"
        ).split("\n")
        if line
        for name, oid, symref in [line.split("\t")]
    }


def head_target(git: Git, view: Path) -> str:
    content = (view / "HEAD").read_text(encoding="utf-8", errors="surrogateescape")
    if content.startswith("ref: "):
        return git_at(git, view, "symbolic-ref", "HEAD").rstrip("\n")
    return git_at(git, view, "rev-parse", "--verify", "HEAD").rstrip("\n")


def transfer(
    git: Git,
    view: Path,
    repo: Path,
    bundle: Path,
    refs: dict[str, tuple[str, str]],
    head: str,
    events: Events | None = None,
) -> None:
    expected = {name: oid for name, (oid, _) in refs.items()}
    head_oid = expected.get(head) if head.startswith("refs/") else head
    if head_oid:
        expected["HEAD"] = head_oid
    if not expected:
        raise RestoreError(
            Result.RESTORE_FAILED, "Empty mirror cannot be exported as a Git bundle"
        )
    git_at(git, view, "bundle", "create", str(bundle), "--all")
    if events is not None:
        events.log("restore.bundle_created")
    # Verify prerequisites before importing anything into a completely empty repo.
    git_at(git, repo, "bundle", "verify", str(bundle))
    listed = [
        line.split(" ", 1)
        for line in git_at(git, repo, "bundle", "list-heads", str(bundle)).split("\n")
        if line
    ]
    if len(listed) != len(expected) or {name: oid for oid, name in listed} != expected:
        raise RestoreError(
            Result.RESTORE_FAILED, "Git bundle reference verification failed"
        )
    if events is not None:
        events.log("restore.bundle_verified")
    refspecs = ["+refs/*:refs/*"] if refs else []
    if head_oid:
        refspecs.append("HEAD")
    git_at(
        git,
        repo,
        "-c",
        "protocol.file.allow=always",
        "fetch",
        "--no-tags",
        "--no-auto-gc",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--update-head-ok",
        "--",
        str(bundle),
        *refspecs,
    )
    if references(git, repo) != {name: (oid, "") for name, (oid, _) in refs.items()}:
        raise RestoreError(
            Result.RESTORE_FAILED, "Offline bundle import changed references"
        )
    if head_oid and not head.startswith("refs/"):
        git_at(git, repo, "update-ref", "--no-deref", "HEAD", head_oid)
    git_at(git, repo, "fsck", "--full", "--no-dangling")


def checkout(
    git: Git,
    repo: Path,
    refs: dict[str, tuple[str, str]],
    head: str,
    branch: str | None,
) -> None:
    if branch is not None:
        head = f"refs/heads/{branch}"
    elif head.startswith("refs/") and head not in refs:
        head = min((r for r in refs if r.startswith("refs/heads/")), default=head)
    # Recreate symbolic refs, which a Git bundle represents as resolved object IDs.
    for name, (_, target) in refs.items():
        if target:
            git_at(git, repo, "symbolic-ref", name, target)
    if head.startswith("refs/") and head not in refs:
        git_at(git, repo, "symbolic-ref", "HEAD", head)
        return
    oid = refs[head][0] if head in refs else head
    git_at(
        git,
        repo,
        "checkout",
        "--no-recurse-submodules",
        "--detach",
        oid,
        "--",
    )
    if head.startswith("refs/heads/"):
        git_at(git, repo, "symbolic-ref", "HEAD", head)


def restore(config: Config, args: Namespace, events: Events | None = None) -> Result:
    event_log = events if events is not None else Events()
    with event_log.run() as started:
        if not config.restore_helper:
            event_log.log(
                "restore.failed",
                logging.ERROR,
                result=Result.FEATURE_DISABLED.name,
                legacy=("Restore commands are disabled (features.restore_helper)",),
            )
            return Result.FEATURE_DISABLED
        return restore_local(config, args, event_log, started)


def restore_local(
    config: Config, args: Namespace, events: Events, started: float | None
) -> Result:
    stop = threading.Event()
    repository = None
    try:
        with ExitStack() as stack:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous = signal.signal(sig, lambda *_: stop.set())
                stack.callback(signal.signal, sig, previous)
            fd = stack.enter_context(locked_root(config.root))
            events.open_file(config)
            mirror = mirror_path(config.root, args.repository)
            repository = args.repository
            events.log("restore.started", repository=repository, kind=args.action)
            cloning = args.action == "clone"
            value = args.destination if cloning else args.output
            parent_fd, target = stack.enter_context(destination(config, value))
            # The directory descriptor pins the checked parent across Git calls.
            stage = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix=".repoharbor-restore-incomplete-",
                        dir=f"/proc/self/fd/{parent_fd}",
                    )
                )
            )
            git = Git(
                Credentials({}),
                stop,
                extra_fds=(parent_fd,),
                output_errors="surrogateescape",
            )
            git.backup_lock_fd = fd
            try:
                view, object_format = source_view(git, mirror, stage)
                refs = references(git, view)
                head = head_target(git, view)
            except GitError:
                raise HarborError(
                    "Local mirror is invalid or contains damaged Git objects"
                ) from None
            branch = args.branch if cloning else None
            if branch is not None:
                try:
                    git_at(git, view, "check-ref-format", f"refs/heads/{branch}")
                except GitError:
                    raise RestoreError(
                        Result.INVALID_ARGUMENT, "Invalid branch name"
                    ) from None
                if f"refs/heads/{branch}" not in refs:
                    raise RestoreError(
                        Result.INVALID_ARGUMENT, "Branch is absent from local mirror"
                    )
            repo = stage / "repository"
            bundle = stage / "export.bundle"
            init_repo(git, repo, object_format, bare=not cloning)
            if not cloning or refs or not head.startswith("refs/"):
                transfer(git, view, repo, bundle, refs, head, events)
            if cloning:
                checkout(git, repo, refs, head, branch)
                artifact = repo
                artifact.chmod(0o700)
                sync_tree(artifact)
            else:
                artifact = bundle
                with artifact.open("rb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    os.fsync(stream.fileno())
            if stop.is_set():
                raise RestoreError(Result.RESTORE_FAILED, "Restore interrupted")
            # A parent moved or replaced since validation must not receive output.
            if Path(f"/proc/self/fd/{parent_fd}").readlink() != target.parent:
                raise RestoreError(
                    Result.DESTINATION_FAILED, "Restore destination parent changed"
                )
            publish(artifact, parent_fd, target.name)
            os.fsync(parent_fd)
    except LogSetupError:
        raise
    except RestoreError as error:
        events.log(
            "restore.failed",
            logging.ERROR,
            started=started,
            repository=repository,
            result=error.result.name,
            kind=args.action,
            legacy=("%s", error),
        )
        return error.result
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "FILESYSTEM_ERROR")
        events.log(
            "restore.failed",
            logging.ERROR,
            started=started,
            repository=repository,
            result=Result.DESTINATION_FAILED.name,
            error_code=code,
            kind=args.action,
            legacy=("Restore filesystem operation failed (%s)", code),
        )
        return Result.DESTINATION_FAILED
    except (HarborError, ValueError, RuntimeError) as error:
        legacy = (
            ("Restore interrupted",)
            if stop.is_set()
            else ("%s", error)
            if isinstance(error, HarborError)
            else ("Invalid restore state or path",)
        )
        events.log(
            "restore.failed",
            logging.ERROR,
            started=started,
            repository=repository,
            result=Result.RESTORE_FAILED.name,
            kind=args.action,
            legacy=legacy,
            error_code="INTERRUPTED" if stop.is_set() else None,
        )
        return Result.RESTORE_FAILED
    events.log(
        "restore.completed",
        started=started,
        repository=repository,
        kind=args.action,
        result="success",
    )
    print(
        "Repository restored successfully"
        if cloning
        else "Git bundle verified and exported"
    )
    return Result.SUCCESS
