"""Actual Git smart HTTP over locally trusted TLS, with real askpass challenges."""

import base64
import os
import socket
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
from repoharbor.errors import GitError
from repoharbor.git_backup import Git
from repoharbor.persistence import Store


def test_https_detached_head_only_update(tmp_path, remote, monkeypatch):
    from repoharbor.config import Config, Source, Storage

    work, bare = remote
    root = tmp_path / "backups"
    root.mkdir()
    with https_remote(tmp_path, bare) as (url, cert, secret, _):
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        cfg = Config(
            (Source("demo", url, credential="local"),),
            ("local",),
            "env",
            root=root,
            storage=Storage(min_free_bytes=0),
        )
        with Store(root) as store:
            engine = Engine(
                cfg,
                store,
                Git(Credentials({"local": secret}), threading.Event(), timeout=20),
            )
            engine.inventory()
            assert urlsplit(url).port is not None
            # A peer that connects without sending a TLS handshake must not
            # prevent Git's separate connection from reaching the fixture.
            with socket.create_connection(("127.0.0.1", urlsplit(url).port), timeout=5):
                engine.run()
            mirror = engine.path("demo") / "repository.git"
            refs = engine.git.refs(mirror)
            (work / "hello.txt").write_text("HTTPS detached\n")
            git("commit", "-am", "detached", cwd=work)
            oid = git("rev-parse", "HEAD", cwd=work)
            git("push", "origin", "HEAD:refs/heads/temporary", cwd=work)
            git("update-ref", "--no-deref", "HEAD", oid, cwd=bare)
            git("update-ref", "-d", "refs/heads/temporary", cwd=bare)
            engine.run()
            assert engine.git.refs(mirror) == refs
            assert engine.git.head(mirror) == oid
            assert engine.git.healthy(mirror)
            assert engine.repos["demo"]["backup"]["successful_updates"] == 2
    git("clone", "--no-hardlinks", mirror, tmp_path / "https-detached-restore")
    assert (
        tmp_path / "https-detached-restore/hello.txt"
    ).read_text() == "HTTPS detached\n"


@contextmanager
def https_remote(tmp_path, bare, hostname="127.0.0.1", secret="test-password"):
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
            "subjectAltName=IP:127.0.0.1,DNS:username.example.org",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    expected = "Basic " + base64.b64encode(f"oauth2:{secret}".encode()).decode()
    state = {
        "status": 200,
        "authorized": 0,
        "challenged": 0,
        "headers": [],
        "location": None,
    }

    class Handler(BaseHTTPRequestHandler):
        timeout = 15

        def log_message(self, *args):
            pass  # No request URL, header or secret ever enters test logs.

        def do_GET(self):
            self.handle_git()

        def do_POST(self):
            self.handle_git()

        def handle_git(self):
            state["headers"].append(
                (
                    self.headers.get("Host"),
                    "Cookie" in self.headers,
                    "Proxy-Authorization" in self.headers,
                )
            )
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
                if state["location"] is not None:
                    self.send_header("Location", state["location"])
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
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(
        server.socket, server_side=True, do_handshake_on_connect=False
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"https://{hostname}:{server.server_port}/{bare.name}",
            cert,
            secret,
            state,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["env", "file"])
@pytest.mark.parametrize("hostname", ["127.0.0.1", "username.example.org"])
def test_https_authentication_and_offline_restore(
    tmp_path, remote, monkeypatch, caplog, mode, hostname
):
    root = tmp_path / "backups"
    root.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    with https_remote(tmp_path, remote[1], hostname) as (url, cert, secret, state):
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        git_env = Credentials.git_env

        def loopback_env(self, name):
            env = git_env(self, name)
            count = int(env["GIT_CONFIG_COUNT"])
            env.update(
                {
                    "GIT_CONFIG_COUNT": str(count + 1),
                    f"GIT_CONFIG_KEY_{count}": "http.curloptResolve",
                    f"GIT_CONFIG_VALUE_{count}": (
                        f"{hostname}:{urlsplit(url).port}:127.0.0.1"
                    ),
                }
            )
            return env

        monkeypatch.setattr(Credentials, "git_env", loopback_env)
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
            token_file = secrets / "local"
            token_file.touch(mode=0o600, exist_ok=False)
            token_file.write_text(secret + "\n")
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


def test_direct_https_ignores_ambient_repository_and_environment(
    tmp_path, remote, monkeypatch
):
    ambient = tmp_path / "ambient"
    git("init", ambient)
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("127.0.0.1\tFALSE\t/\tTRUE\t0\tfixture\tvalue\n")
    for key, value in (
        ("http.extraHeader", "Host: ambient.invalid"),
        ("http.cookieFile", str(cookie)),
        ("http.saveCookies", "true"),
    ):
        git("config", key, value, cwd=ambient)
    monkeypatch.chdir(ambient)
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("KRB5_CONFIG", "KRB5CCNAME", "KRB5_KTNAME", "KRB5_CLIENT_KTNAME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Host: environment.invalid")

    with https_remote(tmp_path, remote[1]) as (url, cert, secret, state):
        transport = Git(Credentials({"local": secret}), threading.Event(), timeout=15)
        child_env = transport.credentials.git_env("local")
        assert not any(
            k.startswith("KRB5") or k.lower().endswith("_proxy") for k in child_env
        )
        # Without the fixture CA, the authenticated endpoint is unreachable.
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        with pytest.raises(GitError, match="GIT_FAILED"):
            transport.remote_refs(url, "local")
        assert not state["headers"]
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        assert "refs/heads/main" in transport.remote_refs(url, "local")
        mirror = tmp_path / "mirror.git"
        transport.clone(url, mirror, "local", lambda: False)
        assert transport.healthy(mirror)
        assert state["authorized"] > 0
        assert set(state["headers"]) == {(urlsplit(url).netloc, False, False)}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("http.extraHeader", "Host: mirror.invalid"),
        ("http.cookieFile", "/dev/null"),
        ("http.saveCookies", "true"),
        ("http.proxy", "http://127.0.0.1:1"),
        ("remote.origin.proxy", "http://127.0.0.1:1"),
        ("http.proxyAuthMethod", "digest"),
        ("http.proxyServiceName", "fixture"),
        ("http.delegation", "always"),
        ("http.followRedirects", "true"),
        ("http.sslVerify", "false"),
        ("protocol.http.allow", "always"),
    ],
)
def test_managed_mirror_rejects_transport_overrides(tmp_path, remote, key, value):
    mirror = tmp_path / "mirror.git"
    git("clone", "--mirror", remote[1], mirror)
    git("config", key, value, cwd=mirror)
    transport = Git(Credentials({}), threading.Event(), timeout=5)
    with pytest.raises(GitError, match="MIRROR_UNSUPPORTED"):
        transport.fetch(mirror, "https://127.0.0.1:1/remote.git", None, lambda: False)


def test_https_rejects_redirects_and_separates_authority_credentials(
    tmp_path, remote, monkeypatch
):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with (
        https_remote(first, remote[1], secret="fixture-first") as a,
        https_remote(second, remote[1], secret="fixture-second") as b,
    ):
        url_a, ca_a, token_a, state_a = a
        url_b, ca_b, token_b, state_b = b
        transport = Git(
            Credentials({"first": token_a, "second": token_b}),
            threading.Event(),
            timeout=15,
        )
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_a))
        state_a.update(status=302, location=url_b)
        with pytest.raises(GitError, match="GIT_FAILED"):
            transport.remote_refs(url_a, "first")
        assert state_a["authorized"] and not state_b["headers"]
        state_a["location"] = url_a.replace("https:", "http:", 1)
        with pytest.raises(GitError, match="GIT_FAILED"):
            transport.remote_refs(url_a, "first")
        assert state_a["authorized"] == 2
        state_a["status"] = 200
        assert "refs/heads/main" in transport.remote_refs(url_a, "first")
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_b))
        assert "refs/heads/main" in transport.remote_refs(url_b, "second")
        assert state_b["challenged"] and state_b["authorized"]


@pytest.mark.parametrize("scheme", ["http", "ftp", "smtp", "ldap", "ssh", "git"])
def test_git_disallows_alternative_network_transports(scheme):
    transport = Git(Credentials({}), threading.Event(), timeout=5)
    with pytest.raises(GitError, match="GIT_FAILED"):
        transport.remote_refs(f"{scheme}://127.0.0.1:1/remote.git", None)


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


@pytest.mark.parametrize("http_status", [401, 403, 404, 429, 503])
def test_https_automatic_retry_only_for_temporary_status(
    factory, tmp_path, remote, monkeypatch, caplog, http_status
):
    from dataclasses import replace

    from repoharbor.config import Source
    from repoharbor.errors import Result
    from repoharbor.recovery import recover

    with https_remote(tmp_path, remote[1]) as (url, cert, secret, server):
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        config = replace(
            factory.config,
            auto_retry=True,
            credentials=("local",),
            sources=(Source("demo", url, credential="local"),),
        )

        def new_git():
            return Git(Credentials({"local": secret}), threading.Event(), timeout=20)

        with factory(config, git_instance=new_git()) as engine:
            engine.retries.clock = lambda: 1000
            server["status"] = http_status
            assert engine.run() == Result.BACKUP_FAILED
            meta = engine.repos["demo"]
            if http_status == 503:
                assert meta["retry"]["reason"] == "GIT_SERVER_TEMPORARY"
                assert meta["retry"]["next_attempt_at"] == 1300
            else:
                assert "retry" not in meta
                assert meta["errors"]["last"]["code"] == "GIT_FAILED"
                if http_status == 429:
                    assert server["authorized"] == 1
        if http_status == 503:
            with factory(config, git_instance=new_git()) as engine:
                server["status"] = 200
                engine.retries.clock = lambda: 1500
                recover(engine)
                assert engine.retries.due() == "demo"
                engine.retries.execute("demo")
                assert engine.retries.due() is None
                meta = engine.repos["demo"]
                assert meta["retry"]["last_result"] == "succeeded"
                assert meta["backup"]["successful_updates"] == 1
                assert meta["errors"]["total"] == 1
                assert (
                    engine.store.data["scheduler"].get("last_successful_run_at") is None
                )
                assert (
                    secret not in (engine.path("demo") / "metadaten.json").read_text()
                )
        assert secret not in caplog.text
