"""Headless service entrypoint."""

import argparse
import errno
import logging
import os
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

from repoharbor import __version__
from repoharbor.config import load
from repoharbor.control import Control, request
from repoharbor.credentials import Credentials
from repoharbor.engine import Engine
from repoharbor.errors import HarborError, Result
from repoharbor.git_backup import Git
from repoharbor.persistence import Store
from repoharbor.recovery import recover
from repoharbor.scheduler import Cron, Scheduler
from repoharbor.selection import repository_id
from repoharbor.status import print_status


def verify_mount(root: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> None:
    # In the image a real mount is mandatory. No auto-mkdir fallback to overlay storage.
    if Path("/.dockerenv").exists():
        mounts = [
            line.split()[4].replace("\\040", " ")
            for line in mountinfo.read_text().splitlines()
        ]
        if str(root) not in mounts:
            raise HarborError("Backup root must be a dedicated persistent bind mount")


def serve(engine: Engine, stop: threading.Event) -> None:
    scheduler = Scheduler(
        Cron(engine.config.schedule),
        engine.config.timezone,
        datetime.now(UTC),
        on_skip=engine.update_skips,
    )
    last_minute = scheduler.last_minute
    workers: list[threading.Thread] = []
    failures: list[Exception] = []

    def tick(now: datetime) -> None:
        try:
            scheduler.tick(now, engine.run)
        except Exception as error:
            failures.append(error)
            stop.set()

    control = Control(engine, scheduler, stop)
    with control.start():
        logging.info("ready; waiting for future schedule (dashboard disabled)")
        while not stop.is_set():
            now = datetime.now(UTC)
            minute = int(now.timestamp() // 60)
            if minute > last_minute:
                last_minute = minute
                # Scheduler observes future ticks while the prior run is still active.
                worker = threading.Thread(target=tick, args=(now,))
                worker.start()
                workers = [t for t in workers if t.is_alive()] + [worker]
            stop.wait(0.25)
        for worker in workers:
            worker.join()
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
                f"Backup worker failed: {code}; journal preserved"
            ) from None
        raise HarborError("Backup worker failed; journal preserved") from None


MESSAGES = {
    Result.INVALID_ARGUMENT: "Invalid arguments",
    Result.UNKNOWN_REPOSITORY: "Unknown repository",
    Result.FEATURE_DISABLED: "Manual backups are disabled (features.manual_run)",
    Result.SERVICE_UNREACHABLE: "Service unreachable or connection lost",
    Result.BUSY: "A backup run is already in progress",
    Result.NOT_SELECTED: "No eligible backup target or repository is excluded",
    Result.DISCOVERY_FAILED: "Discovery failed or is in cooldown",
    Result.BACKUP_FAILED: "Backup failed, was interrupted or could not be completed",
    Result.STATE_UNAVAILABLE: "Backup state is missing or invalid",
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
    return parser.parse_args()


def report(result: Result) -> int:
    if result != Result.SUCCESS:
        logging.error("%s", MESSAGES[result])
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
    try:
        config = load(dict(os.environ))
        if args.command == "backup":
            command = b"all" if repository is None else f"repo {repository}".encode()
            result = (
                request(config.root, command)
                if config.manual_run
                else Result.FEATURE_DISABLED
            )
            if result == Result.SUCCESS:
                print("Backup completed successfully")
            return report(result)
        if args.command in ("status", "repositories"):
            return report(print_status(config, args))

        credentials = Credentials.resolve(config, dict(os.environ))
        if args.command == "config":
            if args.action != "validate":
                raise HarborError("Use config validate")
            print("Configuration valid")
            return 0
        verify_mount(config.root)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        with Store(config.root) as store:
            engine = Engine(config, store, Git(credentials, stop))
            engine.inventory()
            recover(engine)
            serve(engine, stop)
    except HarborError as error:
        logging.error("%s", error)
        return 1
    except OSError as error:
        # No environment, path or remote-controlled exception string is exposed.
        code = {errno.ENOSPC: "ENOSPC", errno.EIO: "EIO", errno.EACCES: "EACCES"}.get(
            error.errno or 0, "FILESYSTEM_ERROR"
        )
        logging.error("%s: startup/state operation failed", code)
        return 1
    return 0
