"""Exercise HTTPS, the HTTP safety boundary and the engine's actual control lock."""

import http.client
import json
import secrets
import ssl
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import pytest

from repoharbor.cli import serve
from repoharbor.config import Web, load
from repoharbor.control import request
from repoharbor.dashboard_data import DashboardData, event_view, snapshot
from repoharbor.dashboard_http import (
    SESSION,
    Sessions,
    dashboard,
    open_live,
)
from repoharbor.errors import HarborError, Result
from repoharbor.events import Events
from repoharbor.persistence import atomic_json, read_json

ORIGIN = "https://127.0.0.1"
HOST = "127.0.0.1"


@pytest.fixture
def tls_files(tmp_path):
    cert, key = tmp_path / "test.crt", tmp_path / "test.key"
    secret_file = tmp_path / "test-password"
    password = secrets.token_urlsafe(32)
    secret_file.write_text(password + "\n")
    secret_file.chmod(0o600)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-out",
            str(cert),
            "-keyout",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    return (
        password,
        ssl.create_default_context(cafile=str(cert)),
        {
            "REPOHARBOR_WEB_PASSWORD_FILE": str(secret_file),
            "REPOHARBOR_WEB_CERT_FILE": str(cert),
            "REPOHARBOR_WEB_KEY_FILE": str(key),
        },
    )


@contextmanager
def listening(server):
    with server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


def exchange(port, method, path, headers=None, value=None, tls=None, raw=None):
    connection = (
        http.client.HTTPSConnection(HOST, port, timeout=10, context=tls)
        if tls
        else http.client.HTTPConnection(HOST, port, timeout=10)
    )
    request_headers = {"Host": HOST, **(headers or {})}
    if value is not None:
        raw = json.dumps(value)
    if raw is not None:
        request_headers.setdefault("Content-Type", "application/json")
    try:
        connection.request(method, path, raw, request_headers)
        response = connection.getresponse()
        body = response.read()
        if response.getheader("Content-Type", "").startswith("application/json"):
            body = json.loads(body)
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def login(port, tls, password):
    status, headers, body = exchange(
        port, "POST", "/api/login", {"Origin": ORIGIN}, {"password": password}, tls
    )
    assert status == 200 and body == {}
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(SESSION + "=")
    assert all(
        part in cookie
        for part in ("Secure", "HttpOnly", "SameSite=Strict", "Max-Age=900")
    )
    cookie_header = {"Cookie": cookie.partition(";")[0]}
    code, headers, data = exchange(
        port, "GET", "/api/dashboard", cookie_header, tls=tls
    )
    assert code == 200 and "demo" not in data
    assert headers["Cache-Control"] == "no-store"
    assert "max-age" in headers["Strict-Transport-Security"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    return {**cookie_header, "Origin": ORIGIN, "X-CSRF-Token": data["csrf"]}


@pytest.mark.parametrize(
    "bad",
    [
        "web: {bind: localhost}",
        "web: {bind: '::1'}",
        "web: {port: true}",
        "web: {port: 80}",
        "web: {port: 65536}",
        "web: {origin: 'http://example.org'}",
        "web: {origin: 'https://example.org/path'}",
        "web: {origin: 'https://user@example.org'}",
        "features: {web_ui: true}",
        "features: {web_ui: 1}",
        "web: {password: omitted}",
    ],
)
def test_web_config_validation(bad):
    with pytest.raises(HarborError):
        load(
            {
                "REPOHARBOR_TOKEN_SOURCE": "env",
                "REPOHARBOR_CONFIG": "schema_version: 1\n" + bad,
            }
        )


def test_schema_one_default_and_explicit_enable(factory):
    config = load(
        {"REPOHARBOR_TOKEN_SOURCE": "env", "REPOHARBOR_CONFIG": "schema_version: 1"}
    )
    assert config.web == Web()
    enabled = load(
        {
            "REPOHARBOR_TOKEN_SOURCE": "env",
            "REPOHARBOR_CONFIG": (
                "schema_version: 1\nfeatures: {web_ui: true}\n"
                "web: {origin: 'https://example.org:8443', port: 8443}"
            ),
        }
    )
    assert enabled.web == Web(True, HOST, 8443, "https://example.org:8443")
    with factory() as engine, dashboard(engine.config, engine.events, {}):
        assert engine.events.recent_events() == []
    config = replace(factory.config, web=enabled.web)
    with factory(config, events=Events(config)) as engine:
        missing = {"REPOHARBOR_WEB_PASSWORD_FILE": str(factory.root / "missing")}
        with dashboard(engine.config, engine.events, missing):
            assert engine.store.data["active_run"] is None
        assert engine.events.recent_events()[-1]["event"] == "web.unavailable"


def test_live_auth_csrf_and_invalid_requests(factory, tls_files):
    password, client, env = tls_files
    config = replace(factory.config, web=Web(True, HOST, 0, ORIGIN))
    with (
        factory(config) as engine,
        listening(open_live(config, engine.events, env)) as port,
    ):
        status, headers, html = exchange(port, "GET", "/", tls=client)
        assert status == 200 and b'lang="en-GB"' in html
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        status, _, body = exchange(port, "GET", "/api/dashboard", tls=client)
        assert status == 401 and body == {"error": "Sign-in required."}
        assert (
            exchange(
                port, "GET", "/api/dashboard", {"Host": "example.org"}, tls=client
            )[0]
            == 403
        )
        assert (
            exchange(
                port,
                "GET",
                "/api/dashboard",
                {"Origin": "https://example.org"},
                tls=client,
            )[0]
            == 403
        )
        assert exchange(port, "GET", "/assets/../../config.py", tls=client)[0] == 404
        assert exchange(port, "GET", "/repositories/%2E%2E", tls=client)[0] == 404
        for route in ("/repositories/demo", "/repositories/github%3Agithub.com%3A123"):
            assert exchange(port, "GET", route, tls=client)[0] == 200
        assert (
            exchange(
                port, "POST", "/api/login", value={"password": password}, tls=client
            )[0]
            == 403
        )
        assert (
            exchange(
                port, "POST", "/api/login", {"Origin": ORIGIN}, raw="[]", tls=client
            )[0]
            == 400
        )
        assert (
            exchange(
                port, "POST", "/api/login", {"Origin": ORIGIN}, raw="{", tls=client
            )[0]
            == 400
        )
        auth = login(port, client, password)
        cookie_only = {"Cookie": auth["Cookie"], "Origin": ORIGIN}
        assert (
            exchange(
                port, "POST", "/api/backup", cookie_only, {"repository": None}, client
            )[0]
            == 403
        )
        status, _, body = exchange(
            port, "POST", "/api/backup", auth, {"repository": None}, client
        )
        assert status == 403 and body == {"error": "Manual backups are disabled."}
        assert exchange(port, "POST", "/api/logout", auth, {}, client)[0] == 200
        assert exchange(port, "GET", "/api/dashboard", auth, tls=client)[0] == 401


def test_session_throttle_and_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("repoharbor.dashboard_http.monotonic", lambda: now[0])
    sessions = Sessions(b"fixture")
    for _ in range(5):
        assert sessions.login("wrong") == (401, None)
    assert sessions.login("fixture") == (429, None)
    now[0] += 61
    code, session = sessions.login("fixture")
    assert code == 200 and sessions.csrf(session)
    now[0] += 901
    assert sessions.csrf(session) is None
    for _ in range(18):
        assert sessions.login("fixture")[0] == 200
    assert len(sessions.active) == 16


def test_real_control_and_busy_responses(factory, tls_files, monkeypatch):
    password, client, env = tls_files
    config = replace(factory.config, web=Web(True, HOST, 0, ORIGIN), manual_run=True)
    stop = Event()
    ready = Event()
    release = Event()
    holder = {}
    with factory(config, events=Events(config)) as engine:
        original = engine.run

        def run(*args, **kwargs):
            ready.set()
            assert release.wait(7)
            return original(*args, **kwargs)

        monkeypatch.setattr(engine, "run", run)

        @contextmanager
        def web(config, events, _):
            server = open_live(config, events, env)
            holder["server"] = server
            with listening(server):
                holder["ready"].set()
                yield

        holder["ready"] = Event()
        monkeypatch.setattr("repoharbor.cli.dashboard", web)
        with ThreadPoolExecutor(max_workers=4) as executor:
            running = executor.submit(serve, engine, stop)
            try:
                assert holder["ready"].wait(4)
                server = holder["server"]
                port = server.server_port
                auth = login(port, client, password)
                for alias in ("../demo", "/tmp/demo", "https://example.org/"):
                    assert (
                        exchange(
                            port,
                            "POST",
                            "/api/backup",
                            auth,
                            {"repository": alias},
                            client,
                        )[0]
                        == 400
                    )
                primary = executor.submit(
                    exchange,
                    port,
                    "POST",
                    "/api/backup",
                    auth,
                    {"repository": "demo"},
                    client,
                )
                assert ready.wait(3)
                assert request(config.root, b"all") == Result.BUSY
                assert (
                    exchange(
                        port,
                        "POST",
                        "/api/backup",
                        auth,
                        {"repository": "demo"},
                        client,
                    )[0]
                    == 409
                )
                server.data.expiry = 0
                concurrent = [
                    executor.submit(
                        exchange, port, "GET", "/api/dashboard", auth, None, client
                    )
                    for _ in range(2)
                ]
                assert all(
                    future.result()[2]["service"]["run_status"] == "running"
                    for future in concurrent
                )
                release.set()
                assert primary.result()[0] == 200
                server.data.expiry = 0
                data = exchange(port, "GET", "/api/dashboard", auth, tls=client)[2]
                assert data["repositories"][0]["backup_status"] == "updated"
                assert data["repositories"][0]["size_measured_at"]
                assert data["service"]["storage"]["free_bytes"] > 0
                assert any(
                    event["operation"] == "backup.completed"
                    for event in data["activities"]
                )
                assert (
                    exchange(
                        port,
                        "POST",
                        "/api/backup",
                        auth,
                        {"repository": "unknown"},
                        client,
                    )[0]
                    == 404
                )
            finally:
                release.set()
                stop.set()
            running.result(timeout=8)
    assert "mirror_path" not in data["repositories"][0]


def test_projection_with_exclusion_dirty_metadata_and_service_absent(factory):
    config = replace(factory.config, web=Web(True, HOST, 0, ORIGIN))
    with factory(config) as engine:
        path = engine.path("demo") / "metadaten.json"
        meta = read_json(path)
        meta["selection"]["status"] = "excluded"
        meta["repository"]["web_url"] = "javascript:void(0)"
        meta["repository"]["name"] = "<unsafe>"
        meta["errors"]["last"] = {
            "at": "2026-01-09T03:01:00+00:00",
            "code": "GITHUB_UNRECOGNIZED",
            "message": "PRIVATE",
        }
        atomic_json(path, meta)
        data = snapshot(config, engine.events)
        assert data["service"]["service_status"] == "unreachable"
        assert data["service"]["repositories"]["excluded"] == 1
        assert data["repositories"][0]["web_url"] is None
        assert data["repositories"][0]["last_error_code"] is None
        assert "PRIVATE" not in json.dumps(data)
        assert "<unsafe>" not in json.dumps(data)
        path.write_text("broken")
        state = snapshot(config, engine.events)
        assert (
            state["repositories"] == []
            and state["service"]["repositories"]["unreadable"] == 1
        )
        journal = config.root / "data.json"
        journal.write_text("broken")
        assert not snapshot(config, engine.events)["state_available"]
        assert journal.read_text() == "broken"


def test_symlink_state_is_ignored(factory, tmp_path):
    config = replace(factory.config, web=Web(True, HOST, 0, ORIGIN))
    with factory(config) as engine:
        path = engine.path("demo") / "metadaten.json"
        original = tmp_path / "unrelated.json"
        original.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(original)
        assert snapshot(config, engine.events)["repositories"] == []


def test_fresh_production_dashboard(factory, tls_files):
    password, client, env = tls_files
    config = replace(factory.config, sources=(), web=Web(True, HOST, 0, ORIGIN))
    with factory(config) as engine, dashboard(config, engine.events, env):
        data = snapshot(config, engine.events)
        assert data["state_available"]
        assert data["repositories"] == data["activities"] == data["sources"] == []
        assert data["service"]["repositories"]["known"] == 0
        assert data["service"]["storage"]["last_measured_bytes"] is None
        assert data["service"]["storage"]["free_bytes"] is None
        assert data["service"]["storage"]["limit_bytes"] is None
        assert data["service"]["last_successful_run_at"] is None
        assert data["service"]["service_status"] == "unreachable"
        assert data["settings"]["logging"]["enabled"] is False
        assert data["settings"]["integrity"]["enabled"] is False
        assert data["settings"]["retry"]["enabled"] is False
        assert data["settings"]["manual_run"] is False
    with (
        factory(config) as engine,
        listening(open_live(config, engine.events, env)) as port,
    ):
        auth = login(port, client, password)
        assert (
            exchange(port, "GET", "/api/dashboard?scenario=large", tls=client)[0] == 401
        )
        assert (
            exchange(port, "GET", "/api/dashboard?scenario=large", auth, tls=client)[0]
            == 400
        )
        for path in (
            "/",
            "/repositories",
            "/activities",
            "/storage",
            "/sources",
            "/settings",
            "/assets/app.js",
            "/assets/style.css",
            "/assets/brand.png",
            "/assets/brand.svg",
            "/favicon.ico",
        ):
            assert exchange(port, "GET", path, tls=client)[0] == 200
        for path in (
            "/demo",
            "/api/demo",
            "/debug",
            "/api/debug",
            "/assets/../data.json",
        ):
            assert exchange(port, "GET", path, auth, tls=client)[0] == 404
        script = exchange(port, "GET", "/assets/app.js", tls=client)[2]
        assert b"Demo scenario" not in script and b"Fictional data" not in script


def test_password_file_validation(factory, tls_files):
    _, _, env = tls_files
    config = replace(factory.config, web=Web(True, HOST, 0, ORIGIN))
    path = Path(env["REPOHARBOR_WEB_PASSWORD_FILE"])
    path.chmod(0o644)
    with pytest.raises(HarborError, match="regular and private"):
        open_live(config, Events(config), env)
    path.chmod(0o600)
    path.write_text("too short")
    with pytest.raises(HarborError, match="URL-safe"):
        open_live(config, Events(config), env)


def test_saved_interruption_and_measured_zero(factory):
    config = replace(factory.config, web=Web(True, HOST, 0, ORIGIN))
    with factory(config) as engine:
        with engine.store.transaction() as state:
            now = datetime.now(UTC).isoformat()
            state["active_run"] = {"state": "interrupted", "started_at": now}
            state["global_storage"] = {
                "last_measured_bytes": 0,
                "free_bytes": 0,
                "last_measured_at": now,
            }
        data = snapshot(config, engine.events)
        assert data["service"]["last_backup_state"] == "interrupted"
        assert data["service"]["active_run"] is None
        assert data["service"]["storage"]["last_measured_bytes"] == 0
        assert data["service"]["storage"]["free_bytes"] == 0


def test_normalized_events_and_cache(factory):
    config = replace(factory.config, web=Web(True, HOST, 0, ORIGIN))
    events = Events(config)
    now = datetime.now(UTC).isoformat()
    assert event_view({"event": "internal", "timestamp": now}) is None
    with events.run():
        events.log(
            "retry.discovery_deferred",
            repository="demo",
            error_code="DISCOVERY_TEMPORARY",
            raw="PRIVATE",
        )
        events.log("backup.failed", error_code="GIT_TIMEOUT")
        events.log("backup.run_started")
    data = DashboardData(config, events)
    saved = data()
    assert [e["status"] for e in saved["activities"]] == ["running", "error", "warning"]
    assert all(e["correlation"] for e in saved["activities"])
    assert "PRIVATE" not in json.dumps(saved)
    assert saved is data()
