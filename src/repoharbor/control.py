"""One local request per Unix socket connection, with no pending backup queue."""

import errno
import os
import socket
import stat
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from repoharbor import integrity
from repoharbor.engine import Engine
from repoharbor.errors import HarborError, Result
from repoharbor.persistence import CONTROL_SOCKET, directory_fd
from repoharbor.scheduler import Scheduler
from repoharbor.selection import repository_id

TIMEOUT = 1.0
MAX_REQUEST = 320


def endpoint(root: Path) -> str:
    return str(root / CONTROL_SOCKET)


def same_user(connection: socket.socket) -> bool:
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _, uid, _ = struct.unpack("iII", credentials)
    return int(uid) == os.geteuid()


def request(root: Path, command: bytes) -> Result:
    try:
        with (
            directory_fd(root) as fd,
            socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client,
        ):
            path = endpoint(Path(f"/proc/self/fd/{fd}"))
            client.settimeout(TIMEOUT)
            client.connect(path)
            if not same_user(client):
                return Result.SERVICE_UNREACHABLE
            client.sendall(command)
            if command != b"status":
                client.settimeout(None)
            return Result(int(client.recv(8)))
    except (OSError, HarborError, ValueError):
        return Result.SERVICE_UNREACHABLE


def reply(connection: socket.socket, result: Result) -> None:
    with suppress(OSError):
        connection.sendall(str(result.value).encode("ascii"))


def parse(command: bytes) -> str | None:
    if command == b"all":
        return None
    if command.startswith(b"repo ") and len(command) <= MAX_REQUEST:
        return repository_id(command[5:].decode("ascii"))
    raise HarborError("Invalid control request")


@contextmanager
def listen(root: Path) -> Iterator[socket.socket]:
    # Store's exclusive root lock protects stale socket cleanup and publication.
    with (
        directory_fd(root) as fd,
        socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server,
    ):
        address = endpoint(Path(f"/proc/self/fd/{fd}"))
        try:
            existing = os.stat(CONTROL_SOCKET, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
                raise HarborError("Unsafe control socket path")
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as previous:
                previous.settimeout(TIMEOUT)
                try:
                    previous.connect(address)
                except ConnectionRefusedError:
                    os.unlink(CONTROL_SOCKET, dir_fd=fd)
                else:
                    raise OSError(errno.EADDRINUSE, "Control socket is already active")
        server.bind(address)
        try:
            os.chmod(CONTROL_SOCKET, 0o600, dir_fd=fd)
            server.listen()
            server.settimeout(0.25)
            yield server
        finally:
            with suppress(FileNotFoundError):
                os.unlink(CONTROL_SOCKET, dir_fd=fd)


class Control:
    def __init__(
        self, engine: Engine, scheduler: Scheduler, stop: threading.Event
    ) -> None:
        self.engine = engine
        self.scheduler = scheduler
        self.stop = stop
        self.worker: threading.Thread | None = None
        self.failure: Exception | None = None

    def run_operation(
        self, connection: socket.socket, repository: str | None, check: bool
    ) -> None:
        with connection:
            try:
                result = (
                    integrity.run(self.engine, manual=True, repository=repository)
                    if check
                    else self.engine.run(manual=True, repository=repository)
                )
            except Exception as error:
                self.failure = error
                self.stop.set()
                result = Result.INTEGRITY_FAILED if check else Result.BACKUP_FAILED
            finally:
                self.scheduler.busy.release()
            reply(connection, result)

    def handle(self, connection: socket.socket) -> bool:
        if not same_user(connection):
            return False
        connection.settimeout(TIMEOUT)
        try:
            command = connection.recv(MAX_REQUEST + 1)
        except OSError:
            return False
        if command == b"status":
            reply(
                connection,
                Result.BUSY if self.scheduler.busy.locked() else Result.SUCCESS,
            )
            return False
        try:
            if len(command) > MAX_REQUEST:
                raise HarborError("Invalid control request")
            check = command.startswith(b"check ")
            repository = parse(command[6:] if check else command)
        except (HarborError, UnicodeError):
            reply(connection, Result.INVALID_ARGUMENT)
            return False
        if self.stop.is_set():
            reply(connection, Result.SERVICE_UNREACHABLE)
        elif not self.engine.config.manual_run:
            reply(connection, Result.FEATURE_DISABLED)
        elif not self.scheduler.busy.acquire(blocking=False):
            reply(connection, Result.BUSY)
        else:
            self.worker = threading.Thread(
                target=self.run_operation, args=(connection, repository, check)
            )
            try:
                self.worker.start()
            except RuntimeError:
                self.scheduler.busy.release()
                raise
            return True
        return False

    def accept(self, server: socket.socket) -> None:
        try:
            while not self.stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                transfer = False
                try:
                    transfer = self.handle(connection)
                finally:
                    if not transfer:
                        connection.close()
        except Exception as error:
            self.failure = error
            self.stop.set()

    @contextmanager
    def start(self) -> Iterator[None]:
        with listen(self.engine.config.root) as server:
            thread = threading.Thread(target=self.accept, args=(server,))
            thread.start()
            try:
                yield
            finally:
                self.stop.set()
                thread.join()
                if self.worker is not None and self.worker.ident is not None:
                    self.worker.join()
