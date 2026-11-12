"""Headless service entrypoint."""

import argparse
import errno
import logging
import os
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path

from repoharbor import __version__
from repoharbor.config import load
from repoharbor.credentials import Credentials
from repoharbor.engine import Engine
from repoharbor.errors import HarborError
from repoharbor.git_backup import Git
from repoharbor.persistence import Store
from repoharbor.recovery import recover
from repoharbor.scheduler import Cron, Scheduler


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


def main() -> int:
    parser = argparse.ArgumentParser(prog="repoharbor")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", nargs="?", choices=["config"])
    parser.add_argument("action", nargs="?", choices=["validate"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load(dict(os.environ))
        credentials = Credentials.resolve(config, dict(os.environ))
        if args.command:
            if args.action != "validate":
                raise HarborError("Use config validate")
            print("Configuration valid")
            return 0
        if args.action:
            raise HarborError("Use config validate")
        verify_mount(config.root)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        with Store(config.root) as store:
            engine = Engine(config, store, Git(credentials, stop))
            engine.inventory()
            recover(engine)
            logging.info("ready; waiting for future schedule (dashboard disabled)")
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
