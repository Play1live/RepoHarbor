"""Authenticated HTTPS transport for the optional dashboard."""

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlsplit

from repoharbor.config import Config
from repoharbor.control import request
from repoharbor.dashboard_data import DashboardData
from repoharbor.errors import HarborError, Result
from repoharbor.events import Events
from repoharbor.selection import repository_id

ASSETS = Path(__file__).with_name("dashboard")
PAGES = {"/", "/repositories", "/activities", "/storage", "/sources", "/settings"}
STATIC = {
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/brand.svg": ("brand.svg", "image/svg+xml"),
    "/assets/brand.png": ("brand.png", "image/png"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
}
SESSION = "__Host-repoharbor"
LIFETIME = 900
LOGIN_WINDOW = 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_PEER_LIMIT = 256
AUTH_ERROR = "Sign-in required."
SocketRequest = socket.socket | tuple[bytes, socket.socket]


def password_verifier(password: bytes, salt: bytes) -> bytes:
    try:
        # OWASP's 32 MiB scrypt profile. maxmem includes OpenSSL's overhead.
        return hashlib.scrypt(
            password, salt=salt, n=2**15, r=8, p=3, maxmem=40 * 1024**2, dklen=32
        )
    except (AttributeError, MemoryError, OSError, ValueError):
        raise HarborError("Dashboard password verification unavailable") from None


class Sessions:
    def __init__(self, credential: bytes):
        self.salt = secrets.token_bytes(16)
        self.verifier = password_verifier(credential, self.salt)
        self.lock = Lock()
        self.verification = Lock()
        self.failures: dict[str, list[float]] = {}
        self.active: dict[str, tuple[float, str]] = {}

    def login(self, credential: str, peer: str) -> tuple[int, str | None]:
        # The same gate bounds scrypt memory and serializes failure accounting.
        # Back-pressure never queues a KDF or blocks existing authenticated requests.
        if not self.verification.acquire(blocking=False):
            return 429, None
        try:
            now = monotonic()
            self.failures = {
                address: recent
                for address, stamps in self.failures.items()
                if (recent := [stamp for stamp in stamps if now - stamp < LOGIN_WINDOW])
            }
            failures = self.failures.get(peer)
            if failures is None and len(self.failures) == LOGIN_PEER_LIMIT:
                return 429, None
            if failures is not None and len(failures) >= LOGIN_FAILURE_LIMIT:
                return 429, None
            try:
                candidate = password_verifier(credential.encode("ascii"), self.salt)
            except HarborError:
                return 503, None
            if not hmac.compare_digest(candidate, self.verifier):
                self.failures.setdefault(peer, []).append(monotonic())
                return 401, None
        finally:
            self.verification.release()

        with self.lock:
            now = monotonic()
            self.active = {key: val for key, val in self.active.items() if val[0] > now}
            if len(self.active) >= 16:
                self.active.pop(next(iter(self.active)))
            session = secrets.token_urlsafe(32)
            self.active[session] = (now + LIFETIME, secrets.token_urlsafe(32))
            return 200, session

    def csrf(self, session: str) -> str | None:
        with self.lock:
            value = self.active.get(session)
            if value is not None and value[0] > monotonic():
                return value[1]
            self.active.pop(session, None)
            return None

    def logout(self, session: str) -> None:
        with self.lock:
            self.active.pop(session, None)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(
        self,
        address: tuple[str, int],
        origin: str,
        data: DashboardData,
        config: Config,
        tls: ssl.SSLContext,
        sessions: Sessions,
    ):
        self.origin = origin
        self.authority = urlsplit(origin).netloc
        self.data = data
        self.config = config
        self.tls = tls
        self.sessions = sessions
        self.slots = BoundedSemaphore(12)
        self.manual = Lock()
        self.assets = {
            route: (mime, (ASSETS / filename).read_bytes())
            for route, (filename, mime) in STATIC.items()
        }
        self.index = (ASSETS / "index.html").read_bytes()
        super().__init__(address, DashboardRequest)

    def process_request(self, request: SocketRequest, client_address: Any) -> None:
        assert isinstance(request, socket.socket)
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(
        self, request: SocketRequest, client_address: Any
    ) -> None:
        assert isinstance(request, socket.socket)
        try:
            request.settimeout(4)
            request = self.tls.wrap_socket(request, server_side=True)
            super().process_request_thread(request, client_address)
        except OSError:
            self.shutdown_request(request)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The standard traceback can include request data and filesystem details.
        pass


class DashboardRequest(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def version_string(self) -> str:
        return "RepoHarbor"

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        self.reply(code, {"error": "The request could not be processed."})

    def header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        return str(values[0]) if len(values) == 1 else None

    def reply(
        self,
        status: int,
        data: dict[str, Any] | bytes,
        mime: str = "application/json; charset=utf-8",
        cookie: str | None = None,
    ) -> None:
        body = (
            data
            if isinstance(data, bytes)
            else json.dumps(data, allow_nan=False).encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'",
        )
        self.send_header("Strict-Transport-Security", "max-age=86400")
        if status == 429:
            self.send_header("Retry-After", str(LOGIN_WINDOW))
        if cookie is not None:
            self.send_header(
                "Set-Cookie",
                f"{SESSION}={cookie}; Path=/; Secure; HttpOnly; SameSite=Strict",
            )
        self.end_headers()
        self.wfile.write(body)

    def trusted(self) -> bool:
        origins = self.headers.get_all("Origin", [])
        return self.header("Host") == self.server.authority and (
            origins == [self.server.origin]
            if self.command == "POST"
            else not origins or origins == [self.server.origin]
        )

    def session(self) -> str:
        jar = SimpleCookie()
        try:
            jar.load(self.header("Cookie") or "")
        except CookieError:
            return ""
        value = jar[SESSION].value if SESSION in jar else ""
        return value if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) else ""

    def do_GET(self) -> None:
        if not self.trusted():
            self.reply(403, {"error": "Access denied."})
            return
        route, query, _ = self.path.partition("?")
        if route in self.server.assets:
            mime, data = self.server.assets[route]
            self.reply(200, data, mime)
        elif route in PAGES or repository_page(route):
            self.reply(200, self.server.index, "text/html; charset=utf-8")
        elif route == "/api/dashboard":
            csrf = self.server.sessions.csrf(self.session())
            if csrf is None:
                self.reply(401, {"error": AUTH_ERROR})
                return
            if query:
                self.reply(400, {"error": "Invalid request."})
                return
            try:
                payload = self.server.data()
                body = json.dumps({**payload, "csrf": csrf}, allow_nan=False).encode()
            except (HarborError, OSError, TypeError, ValueError, OverflowError):
                self.reply(503, {"error": "Status data is currently unavailable."})
            else:
                self.reply(200, body)
        else:
            self.reply(404, {"error": "Page not found."})

    def body(self) -> dict[str, Any] | None:
        size = self.header("Content-Length")
        if (
            self.headers.get_all("Transfer-Encoding")
            or self.header("Content-Type") != "application/json"
            or size is None
            or not re.fullmatch(r"[1-9][0-9]{0,2}", size)
        ):
            return None
        try:
            data = json.loads(self.rfile.read(int(size)))
        except (ValueError, RecursionError):
            return None
        return data if isinstance(data, dict) else None

    def do_POST(self) -> None:
        sessions = self.server.sessions
        if self.path not in {
            "/api/login",
            "/api/logout",
            "/api/backup",
        }:
            self.reply(404, {"error": "Action unavailable."})
            return
        if not self.trusted():
            self.reply(403, {"error": "Access denied."})
            return
        session = self.session()
        if self.path != "/api/login":
            csrf = sessions.csrf(session)
            supplied = self.header("X-CSRF-Token") or ""
            if (
                csrf is None
                or not supplied.isascii()
                or not hmac.compare_digest(csrf, supplied)
            ):
                self.reply(403, {"error": "Invalid session or request."})
                return
        body = self.body()
        if body is None:
            self.reply(400, {"error": "Invalid request."})
        elif self.path == "/api/login":
            self.login(sessions, session, body)
        elif self.path == "/api/logout" and not body:
            sessions.logout(session)
            self.reply(200, {}, cookie="; Max-Age=0")
        elif self.path == "/api/backup":
            self.backup(body)
        else:
            self.reply(400, {"error": "Invalid request."})

    def login(self, sessions: Sessions, session: str, body: dict[str, Any]) -> None:
        password = body.get("password")
        if (
            set(body) != {"password"}
            or not isinstance(password, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", password)
        ):
            self.reply(400, {"error": "Invalid sign-in request."})
            return
        # Only the TCP peer is authoritative. A proxy's forwarding headers are ignored.
        status, token = sessions.login(password, self.client_address[0])
        if token is not None:
            sessions.logout(session)
            self.reply(200, {}, cookie=f"{token}; Max-Age={LIFETIME}")
        else:
            message = {
                429: "Too many sign-in attempts. Try again shortly.",
                503: "Sign-in is currently unavailable.",
            }.get(status, "Sign-in failed.")
            self.reply(status, {"error": message})

    def backup(self, body: dict[str, Any]) -> None:
        config = self.server.config
        if not config.manual_run:
            self.reply(403, {"error": "Manual backups are disabled."})
            return
        alias = body.get("repository")
        try:
            if set(body) != {"repository"} or (
                alias is not None and not isinstance(alias, str)
            ):
                raise HarborError("Invalid target")
            command = (
                b"all"
                if alias is None
                else f"repo {repository_id(alias)}".encode("ascii")
            )
        except (HarborError, ValueError):
            self.reply(400, {"error": "Invalid repository."})
            return
        # Bound HTTP submissions while the control socket owns the actual shared lock.
        if not self.server.manual.acquire(blocking=False):
            result = Result.BUSY
        else:
            try:
                result = request(config.root, command)
            finally:
                self.server.manual.release()
        status, message = BACKUP_RESULTS.get(result, (500, "Backup failed."))
        self.reply(status, {"message" if status == 200 else "error": message})


BACKUP_RESULTS = {
    Result.SUCCESS: (200, "Backup completed successfully."),
    Result.BUSY: (409, "A backup or check is already in progress."),
    Result.UNKNOWN_REPOSITORY: (404, "Unknown repository."),
    Result.NOT_SELECTED: (409, "No repository is eligible for backup."),
    Result.FEATURE_DISABLED: (403, "Manual backups are disabled."),
    Result.SERVICE_UNREACHABLE: (503, "The backup service is unavailable."),
    Result.DISCOVERY_FAILED: (409, "Discovery failed or is in cooldown."),
}


def repository_page(path: str) -> bool:
    prefix = "/repositories/"
    if not path.startswith(prefix) or len(path) > len(prefix) + 400:
        return False
    try:
        repository_id(unquote(path[len(prefix) :], errors="strict"))
    except (HarborError, UnicodeError):
        return False
    return True


def read_password(path: Path) -> bytes:
    if not path.is_absolute():
        raise HarborError("Web password file path must be absolute")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise HarborError("Web password file must be regular and private")
        password = stream.read(131).removesuffix(b"\n").removesuffix(b"\r")
    if not re.fullmatch(rb"[A-Za-z0-9_-]{43,128}", password):
        raise HarborError("Web password must be a generated URL-safe secret")
    return password


def open_live(
    config: Config, events: Events, env: Mapping[str, str]
) -> DashboardServer:
    web = config.web
    if not web.enabled or web.origin is None:
        raise HarborError("Web dashboard is disabled")
    password_file = Path(
        env.get("REPOHARBOR_WEB_PASSWORD_FILE", "/run/secrets/repoharbor-web-password")
    )
    cert = Path(env.get("REPOHARBOR_WEB_CERT_FILE", "/run/secrets/repoharbor-web-cert"))
    key = Path(env.get("REPOHARBOR_WEB_KEY_FILE", "/run/secrets/repoharbor-web-key"))
    if not all(path.is_absolute() for path in (cert, key)):
        raise HarborError("Web certificate and key paths must be absolute")
    if not stat.S_ISREG(cert.stat().st_mode):
        raise HarborError("Web certificate must be a regular file")
    key_mode = key.lstat().st_mode
    if not stat.S_ISREG(key_mode) or key_mode & 0o077:
        raise HarborError("Web private key must be a regular and private file")
    password = read_password(password_file)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert, key, password=lambda: "")
    return DashboardServer(
        (web.bind, web.port),
        web.origin,
        DashboardData(config, events),
        config,
        tls,
        Sessions(password),
    )


@contextmanager
def dashboard(config: Config, events: Events, env: Mapping[str, str]) -> Iterator[None]:
    if not config.web.enabled:
        yield
        return
    try:
        server = open_live(config, events, env)
    except (HarborError, OSError, ValueError):
        events.log("web.unavailable", legacy=("Web dashboard could not open securely",))
        yield
        return
    with server:
        thread = Thread(target=server.serve_forever, name="repoharbor-web")
        thread.start()
        try:
            events.log("web.ready", legacy=("HTTPS dashboard ready",))
            yield
        finally:
            server.shutdown()
            thread.join()
