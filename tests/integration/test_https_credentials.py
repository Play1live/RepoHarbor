"""Actual Git smart HTTP over locally trusted TLS, with real askpass challenges."""

import base64
import os
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import yaml
from conftest import git

from repoharbor import cli
from repoharbor.config import load
from repoharbor.credentials import Credentials
from repoharbor.engine import Engine
from repoharbor.git_backup import Git
from repoharbor.persistence import Store


@contextmanager
def https_remote(tmp_path, bare):
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    secret = "test-password"
    expected = "Basic " + base64.b64encode(f"oauth2:{secret}".encode()).decode()
    state = {"status": 200, "authorized": 0, "challenged": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No request URL, header or secret ever enters test logs.

        def do_GET(self):
            self.handle_git()

        def do_POST(self):
            self.handle_git()

        def handle_git(self):
            if self.headers.get("Authorization") != expected:
                state["challenged"] += 1
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="local test"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            state["authorized"] += 1
            if state["status"] != 200:
                self.send_response(state["status"])
                self.send_header("Content-Length", str(len(secret.encode())))
                self.end_headers()
                self.wfile.write(
                    secret.encode()
                )  # Remote-controlled error is redacted.
                return
            url = urlsplit(self.path)
            env = {
                "PATH": os.environ["PATH"],
                "GIT_PROJECT_ROOT": str(bare.parent),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": url.path,
                "QUERY_STRING": url.query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "REMOTE_USER": "oauth2",
            }
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            response = subprocess.run(
                ["git", "http-backend"],
                input=body,
                env=env,
                capture_output=True,
                timeout=15,
                check=True,
            ).stdout
            headers, payload = response.split(b"\r\n\r\n", 1)
            parsed = [line.decode().split(": ", 1) for line in headers.split(b"\r\n")]
            status = next((int(v.split()[0]) for k, v in parsed if k == "Status"), 200)
            self.send_response(status)
            for name, value in parsed:
                if name != "Status":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}/{bare.name}", cert, secret, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["env", "file"])
def test_https_authentication_and_offline_restore(
    tmp_path, remote, monkeypatch, caplog, mode
):
    root = tmp_path / "backups"
    root.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    with https_remote(tmp_path, remote[1]) as (url, cert, secret, state):
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        env = {
            "REPOHARBOR_TOKEN_SOURCE": mode,
            "REPOHARBOR_BACKUP_ROOT": str(root),
            "REPOHARBOR_SECRETS_DIR": str(secrets),
            "REPOHARBOR_CONFIG": yaml.safe_dump(
                {
                    "schema_version": 1,
                    "credentials": ["local"],
                    "manual_git_urls": [
                        {"alias": "demo", "url": url, "credential": "local"}
                    ],
                    "storage": {"min_free_gib": 0},
                }
            ),
        }
        if mode == "file":
            (secrets / "local").write_text(secret + "\n")
        else:
            env["REPOHARBOR_TOKEN_LOCAL"] = secret
        cfg = load(env)
        credentials = Credentials.resolve(cfg, env)
        with Store(root) as store:
            engine = Engine(cfg, store, Git(credentials, threading.Event(), timeout=20))
            engine.inventory()
            engine.run()
            meta = engine.repos["demo"]
            assert meta["backup"]["successful_updates"] == 1
            assert state["authorized"] > 0 and state["challenged"] > 0
            old = meta["backup"]["last_updated_at"]
            state["status"] = 403
            engine.run()
            assert meta["source"]["status"] == "unavailable"
            assert meta["backup"]["last_updated_at"] == old
            state["status"] = 200
            engine.run()
            assert meta["source"]["status"] == "available"
            assert meta["errors"]["consecutive"] == 0
            mirror = engine.path("demo") / "repository.git"
            for path in (
                root / "data.json",
                engine.path("demo") / "metadaten.json",
                mirror / "config",
            ):
                assert secret not in path.read_text()
            assert secret not in caplog.text
    # Server is gone. Restore must still work without authentication or network.
    git("clone", "--no-hardlinks", mirror, tmp_path / "restored")
    assert (tmp_path / "restored/hello.txt").read_text() == "first\n"


def test_config_validate_has_no_git_or_runtime_mutations(tmp_path, monkeypatch, capsys):
    root = tmp_path / "must-not-be-created"
    monkeypatch.setenv("REPOHARBOR_TOKEN_SOURCE", "env")
    monkeypatch.setenv("REPOHARBOR_BACKUP_ROOT", str(root))
    monkeypatch.setenv(
        "REPOHARBOR_CONFIG",
        yaml.safe_dump(
            {
                "schema_version": 1,
                "manual_git_urls": [
                    {"alias": "demo", "url": "https://example.org/repo.git"}
                ],
            }
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Validation must not instantiate Git or open Store")

    monkeypatch.setattr(cli, "Git", forbidden)
    monkeypatch.setattr(cli, "Store", forbidden)
    monkeypatch.setattr(sys, "argv", ["repoharbor", "config", "validate"])
    assert cli.main() == 0
    assert capsys.readouterr().out == "Configuration valid\n"
    assert not root.exists()
