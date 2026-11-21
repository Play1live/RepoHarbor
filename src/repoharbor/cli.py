"""Headless service entrypoint."""

import argparse
import errno
import logging
import os
import signal
import stat
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

from repoharbor import __version__, integrity
from repoharbor.config import load
from repoharbor.control import Control, request
from repoharbor.credentials import Credentials
from repoharbor.dashboard_http import dashboard
from repoharbor.engine import Engine
from repoharbor.errors import HarborError, Result
from repoharbor.events import Events
from repoharbor.git_backup import Git
from repoharbor.log_files import LogSetupError
from repoharbor.mounts import read_mounts
from repoharbor.persistence import STORAGE_MARKER, Store, directory_fd
from repoharbor.recovery import recover
from repoharbor.restore import restore
from repoharbor.scheduler import Cron, Scheduler
from repoharbor.selection import repository_id
from repoharbor.status import print_status


def verify_mount(root: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> None:
    # In the image a real mount is mandatory. No auto-mkdir fallback to overlay storage.
    if Path("/.dockerenv").exists():
        mount = read_mounts(mountinfo).get(root)
        if mount is None or mount.filesystem in (b"tmpfs", b"ramfs"):
            raise HarborError("Backup root must be a dedicated persistent bind mount")
    expected = os.environ.get("REPOHARBOR_STORAGE_ID")
    if expected is not None:
        if not expected.isascii() or not (1 <= len(expected) <= 128):
            raise HarborError("Invalid backup storage identifier")
        with (
            directory_fd(root) as fd,
            os.fdopen(
                os.open(
                    STORAGE_MARKER,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=fd,
                ),
                "rb",
            ) as marker,
        ):
            if not stat.S_ISREG(os.fstat(marker.fileno()).st_mode):
                raise HarborError("Backup storage marker must be a regular file")
            if marker.read(130).removesuffix(b"\n") != expected.encode("ascii"):
                raise HarborError("Backup storage marker does not match configuration")


def serve(engine: Engine, stop: threading.Event) -> None:
    initial = datetime.now(UTC)
    scheduler = Scheduler(
        Cron(engine.config.schedule),
        engine.config.timezone,
        initial,
        on_skip=engine.update_skips,
    )
    checker = (
        Scheduler(
            Cron(engine.config.integrity.schedule),
            engine.config.timezone,
            initial,
            on_skip=lambda skipped: integrity.skip_overlap(
                engine.store, skipped, engine.events
            ),
            busy=scheduler.busy,
        )
        if engine.config.integrity_check
        else None
    )
    worker: threading.Thread | None = None
    failures: list[Exception] = []

    def record_failure(error: Exception) -> None:
        failures.append(error)
        stop.set()

    def run(operation: Callable[[], object]) -> None:
        if not stop.is_set():
            try:
                operation()
            except Exception as error:
                record_failure(error)

    def dispatch(operation: Callable[[], None]) -> None:
        nonlocal worker
        candidate = threading.Thread(target=operation)
        candidate.start()
        worker = candidate

    def start_retry() -> None:
        if not engine.config.auto_retry or not scheduler.busy.acquire(blocking=False):
            return
        handed_off = False
        try:
            alias = engine.retries.due()
            if alias is None:
                return

            def execute() -> None:
                try:
                    run(lambda: engine.retries.execute(alias))
                finally:
                    scheduler.busy.release()

            dispatch(execute)
            handed_off = True
        finally:
            if not handed_off:
                scheduler.busy.release()

    control = Control(engine, scheduler, stop)
    with control.start(), dashboard(engine.config, engine.events, os.environ):
        engine.events.log(
            "service.ready",
            legacy=("ready; waiting for future schedule",),
        )
        try:
            while not stop.is_set():
                now = datetime.now(UTC)
                scheduler.tick(now, lambda: run(engine.run), dispatch)
                if checker is not None and not stop.is_set():
                    checker.tick(
                        now, lambda: run(lambda: integrity.run(engine)), dispatch
                    )
                if not stop.is_set():
                    start_retry()
                stop.wait(0.25)
        except Exception as error:
            record_failure(error)
        finally:
            stop.set()
            if worker is not None:
                worker.join()
            engine.events.log("service.stopped")
    if control.failure is not None:
        failures.append(control.failure)
    if failures:
        failure = failures[0]
        if isinstance(failure, OSError):
            code = {
                errno.ENOSPC: "ENOSPC",
                errno.EIO: "EIO",
                errno.EACCES: "EACCES",
            }.get(failure.errno or 0, "FILESYSTEM_ERROR")
            raise HarborError(
                f"Service worker failed: {code}; journal preserved"
            ) from None
        raise HarborError("Service worker failed; journal preserved") from None


MESSAGES = {
    Result.INVALID_ARGUMENT: "Invalid arguments",
    Result.UNKNOWN_REPOSITORY: "Unknown repository",
    Result.FEATURE_DISABLED: "Manual backups are disabled (features.manual_run)",
    Result.SERVICE_UNREACHABLE: "Service unreachable or connection lost",
    Result.BUSY: "A backup or integrity check is already in progress",
    Result.NOT_SELECTED: "No eligible backup target or repository is excluded",
    Result.DISCOVERY_FAILED: "Discovery failed or is in cooldown",
    Result.BACKUP_FAILED: "Backup failed, was interrupted or could not be completed",
    Result.STATE_UNAVAILABLE: "Backup state is missing or invalid",
    Result.RESTORE_FAILED: "Restore or bundle export failed",
    Result.DESTINATION_FAILED: "Restore destination is unavailable",
}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse diagnostics can include user-supplied arguments verbatim.
        super().error("invalid arguments")


def arguments() -> argparse.Namespace:
    parser = Parser(prog="repoharbor")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    config = commands.add_parser("config")
    config.add_argument("action", nargs="?", choices=["validate"])
    commands.add_parser("status").add_argument("--json", action="store_true")
    repos = commands.add_parser("repositories").add_subparsers(
        dest="action", required=True
    )
    repos.add_parser("list").add_argument("--json", action="store_true")
    show = repos.add_parser("show")
    show.add_argument("repository")
    show.add_argument("--json", action="store_true")
    run = (
        commands.add_parser("backup")
        .add_subparsers(dest="action", required=True)
        .add_parser("run")
    )
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true")
    target.add_argument("--repository")
    recovery = commands.add_parser(
        "restore", help="Restore or export a local backup while the service is stopped"
    ).add_subparsers(dest="action", required=True)
    clone = recovery.add_parser("clone", help="Restore a mirror to a new working tree")
    clone.add_argument(
        "--repository", required=True, help="Local alias or stable provider ID"
    )
    clone.add_argument(
        "--destination",
        required=True,
        help="New directory outside backup and secret paths",
    )
    clone.add_argument("--branch", help="Existing branch to check out")
    bundle = recovery.add_parser(
        "bundle", help="Export and verify a standalone Git bundle"
    )
    bundle.add_argument(
        "--repository", required=True, help="Local alias or stable provider ID"
    )
    bundle.add_argument(
        "--output",
        required=True,
        help="New bundle file outside backup and secret paths",
    )
    return parser.parse_args()


def report(result: Result, events: Events | None = None) -> int:
    if result != Result.SUCCESS:
        (events if events is not None else Events()).log(
            "command.failed",
            logging.ERROR,
            result=result.name,
            legacy=("%s", MESSAGES[result]),
        )
    return result.value


def main() -> int:
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    repository = getattr(args, "repository", None)
    try:
        if repository is not None:
            repository_id(repository)
    except HarborError:
        return report(Result.INVALID_ARGUMENT)
    events = Events()
    stage = "configuration"
    failure_event = (
        "service.startup_failed" if args.command is None else "command.failed"
    )
    try:
        config = load(dict(os.environ))
        events = Events(config)
        if args.command == "backup":
            command = b"all" if repository is None else f"repo {repository}".encode()
            result = (
                request(config.root, command)
                if config.manual_run
                else Result.FEATURE_DISABLED
            )
            if result == Result.SUCCESS:
                print("Backup completed successfully")
            return report(result, events)
        if args.command in ("status", "repositories"):
            return report(print_status(config, args), events)
        if args.command == "restore":
            stage = "backup_mount"
            if config.restore_helper:
                verify_mount(config.root)
            return restore(config, args, events).value

        if args.command is None:
            events.open_file(config)
            events.log("service.started")
        stage = "credentials"
        credentials = Credentials.resolve(config, dict(os.environ))
        if args.command == "config":
            if args.action != "validate":
                raise HarborError("Use config validate")
            print("Configuration valid")
            return 0
        stage = "backup_mount"
        verify_mount(config.root)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        stage = "backup_state"
        with Store(config.root) as store:
            engine = Engine(config, store, Git(credentials, stop), events=events)
            engine.inventory()
            with events.run():
                recover(engine)
                integrity.reconcile(store, events)
            stage = "serving"
            failure_event = "service.failed"
            serve(engine, stop)
    except LogSetupError as error:
        events.log(
            failure_event, logging.CRITICAL, error_code=error.code, result="log_setup"
        )
        return 1
    except HarborError as error:
        events.log(failure_event, logging.ERROR, result=stage, legacy=("%s", error))
        return 1
    except OSError as error:
        # No environment, path or remote-controlled exception string is exposed.
        code = {errno.ENOSPC: "ENOSPC", errno.EIO: "EIO", errno.EACCES: "EACCES"}.get(
            error.errno or 0, "FILESYSTEM_ERROR"
        )
        events.log(
            failure_event,
            logging.ERROR,
            result=stage,
            error_code=code,
            legacy=("%s: startup/state operation failed", code),
        )
        return 1
    except Exception:
        if events.logger is None:
            raise
        events.log(
            failure_event, logging.ERROR, result=stage, error_code="OPERATION_FAILED"
        )
        return 1
    finally:
        events.close()
    return 0
