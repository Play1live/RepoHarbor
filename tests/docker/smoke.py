"""Standalone Docker smoke harness; run after building repoharbor:local-test."""

import json
import select
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from dashboard_smoke import dashboard_smoke

IMAGE = "repoharbor:local-test"


def docker(*args):
    return subprocess.check_output(
        ["docker", *args], text=True, stderr=subprocess.STDOUT, timeout=60
    ).strip()


def exec_result(container, *args):
    return subprocess.run(
        ["docker", "exec", container, "repoharbor", *args],
        text=True,
        capture_output=True,
        timeout=30,
    )


def wait_ready(container):
    deadline = time.monotonic() + 30
    while "ready;" not in docker("logs", container):
        state = json.loads(docker("inspect", container))[0]["State"]
        if not state["Running"] or time.monotonic() >= deadline:
            raise AssertionError(docker("logs", container))
        threading.Event().wait(0.1)


def no_tcp_listener(container):
    check = "from pathlib import Path; " + (
        "assert not any(line.split()[3]=='0A' "
        "for p in ('/proc/net/tcp','/proc/net/tcp6') "
        "for line in Path(p).read_text().splitlines()[1:])"
    )
    docker("exec", container, "python", "-c", check)


def restore_smoke(container, mount, fixture):
    with tempfile.TemporaryDirectory() as output:
        Path(output).chmod(0o777)
        output_mount = f"type=bind,src={output},dst=/exports"
        try:
            args = [
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "-e",
                "REPOHARBOR_TOKEN_SOURCE=file",
                "-e",
                "REPOHARBOR_CONFIG=schema_version: 1\n"
                "features: {restore_helper: true}\ncredentials: [unused]",
                "--mount",
                mount + ",readonly",
                "--mount",
                output_mount,
                IMAGE,
                "restore",
            ]
            clone = [
                "clone",
                "--repository",
                "demo",
                "--destination",
                "/exports/restore",
            ]
            busy = subprocess.run(
                ["docker", *args, *clone], capture_output=True, text=True, timeout=30
            )
            assert busy.returncode == 6 and "Backup root is locked" in busy.stderr
            assert list(Path(output).iterdir()) == []
            docker("stop", "-t", "15", container)
            assert json.loads(docker("inspect", container))[0]["State"]["ExitCode"] == 0
            assert docker(*args, *clone) == "Repository restored successfully"
            assert docker(
                *args,
                "bundle",
                "--repository",
                "demo",
                "--output",
                "/exports/demo.bundle",
            ) == ("Git bundle verified and exported")
            commit = subprocess.check_output(
                ["git", "-C", str(fixture / "work"), "rev-parse", "HEAD"], text=True
            ).strip()
            assert (
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    output_mount,
                    "--entrypoint",
                    "python",
                    IMAGE,
                    "-c",
                    "import subprocess; from pathlib import Path; "
                    "assert Path('/exports/restore/hello.txt').read_text() == "
                    "'test fixture\\n'; "
                    "subprocess.run(['git', 'clone', '/exports/demo.bundle', "
                    "'/tmp/from-bundle'], "
                    "check=True, capture_output=True); "
                    "print(subprocess.check_output(['git', '-C', '/tmp/from-bundle', "
                    "'rev-parse', 'HEAD'], text=True).strip()); "
                    "print(subprocess.check_output(['git', '-C', '/exports/restore', "
                    "'rev-parse', 'HEAD'], text=True).strip())",
                )
                == f"{commit}\n{commit}"
            )
        finally:
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                output_mount,
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                "from pathlib import Path; import shutil; "
                "shutil.rmtree('/exports/restore', ignore_errors=True); "
                "Path('/exports/demo.bundle').unlink(missing_ok=True)",
            )


def manual_smoke(mount):
    # A real bare Git remote served over TLS on the test container's loopback.
    with tempfile.TemporaryDirectory() as directory:
        fixture = Path(directory)
        fixture.chmod(0o755)

        def git(*args):
            subprocess.run(
                ["git", "-C", directory, *args],
                check=True,
                capture_output=True,
                timeout=15,
            )

        git("init", "-b", "main", "work")
        (fixture / "work/hello.txt").write_text("test fixture\n")
        git("-C", "work", "add", "hello.txt")
        git("-C", "work", "config", "user.name", "Example")
        git("-C", "work", "config", "user.email", "test@example.org")
        git("-C", "work", "commit", "-m", "fixture")
        git("clone", "--bare", "work", "remote.git")
        git("-C", "remote.git", "update-server-info")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-out",
                "cert.pem",
                "-keyout",
                "key.pem",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            cwd=fixture,
            check=True,
            capture_output=True,
            timeout=15,
        )
        # The test host can have umask 077 while the service uses UID 10001.
        for path in [
            fixture / "cert.pem",
            fixture / "key.pem",
            fixture / "remote.git",
            *(fixture / "remote.git").rglob("*"),
        ]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        config = (
            "schema_version: 1\nschedule: '0 0 29 2 *'\n"
            "features: {manual_run: true, auto_retry: true}\n"
            "retry: {max_retries: 3, initial_delay_seconds: 3}\n"
            "manual_git_urls:\n  - alias: demo\n"
            "    url: https://127.0.0.1:8443/remote.git\n"
            "storage: {min_free_gib: 0}\n"
        )
        env = ["-e", "REPOHARBOR_TOKEN_SOURCE=env", "-e", f"REPOHARBOR_CONFIG={config}"]
        container = docker(
            "run",
            "-d",
            "--network",
            "none",
            *env,
            "-e",
            "SSL_CERT_FILE=/fixture/cert.pem",
            "--mount",
            mount,
            "--mount",
            f"type=bind,src={fixture},dst=/fixture,readonly",
            IMAGE,
        )
        fixture_process = None
        try:
            wait_ready(container)
            no_tcp_listener(container)
            status = json.loads(
                docker("exec", container, "repoharbor", "status", "--json")
            )
            assert (
                status["service_status"] == "running"
                and status["last_run_started_at"] is None
            )
            duplicate = subprocess.run(
                ["docker", "run", "--rm", *env, "--mount", mount, IMAGE],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert duplicate.returncode == 1
            assert "startup/state operation failed" in duplicate.stderr
            fixture_code = """
import http.server, os, ssl
os.chdir('/fixture')
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if os.path.exists('outage'):
            self.send_error(503)
        else:
            super().do_GET()
server = http.server.ThreadingHTTPServer(('127.0.0.1', 8443), Handler)
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain('cert.pem', 'key.pem')
server.socket = tls.wrap_socket(server.socket, server_side=True)
print("TLS fixture ready", flush=True)
server.serve_forever()
"""
            fixture_process = subprocess.Popen(
                ["docker", "exec", container, "python", "-c", fixture_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert select.select([fixture_process.stdout], [], [], 15)[0]
            ready = fixture_process.stdout.readline().strip()
            if ready != "TLS fixture ready":
                fixture_process.wait(timeout=15)
                raise AssertionError(ready + fixture_process.stdout.read())
            first = exec_result(container, "backup", "run", "--all")
            assert first.returncode == 0, (first.stderr, docker("logs", container))
            before = json.loads(
                docker(
                    "exec",
                    container,
                    "repoharbor",
                    "repositories",
                    "show",
                    "demo",
                    "--json",
                )
            )
            assert before["successful_updates"] == 1
            again = exec_result(container, "backup", "run", "--repository", "demo")
            assert again.returncode == 0, again.stderr
            after = json.loads(
                docker(
                    "exec",
                    container,
                    "repoharbor",
                    "repositories",
                    "show",
                    "demo",
                    "--json",
                )
            )
            assert after["successful_updates"] == 1
            assert after["last_updated_at"] == before["last_updated_at"]
            (fixture / "outage").touch()
            failed = exec_result(container, "backup", "run", "--repository", "demo")
            assert failed.returncode == 9, failed.stderr
            (fixture / "outage").unlink()
            deadline = time.monotonic() + 20
            while True:
                after_retry = json.loads(
                    docker(
                        "exec",
                        container,
                        "repoharbor",
                        "repositories",
                        "show",
                        "demo",
                        "--json",
                    )
                )
                if after_retry["retry"]["last_result"] == "succeeded":
                    break
                assert time.monotonic() < deadline, docker("logs", container)
                time.sleep(0.15)
            assert after_retry["successful_updates"] == 1
            assert after_retry["retry"]["reason"] == "GIT_SERVER_TEMPORARY"
            assert after_retry["errors"]["total"] == 1
            docker(
                "exec",
                container,
                "git",
                "clone",
                "--no-hardlinks",
                "/backups/manual/demo/repository.git",
                "/tmp/restore",
            )
            assert (
                docker("exec", container, "cat", "/tmp/restore/hello.txt")
                == "test fixture"
            )
            restore_smoke(container, mount, fixture)
            integrity_smoke(mount)
        finally:
            docker("rm", "-f", container)
            if fixture_process is not None:
                fixture_process.communicate(timeout=15)
            docker(
                "run",
                "--rm",
                "--mount",
                mount,
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                "import shutil; shutil.rmtree('/backups/manual', ignore_errors=True)",
            )


def integrity_smoke(mount):
    seed = """
from pathlib import Path
import json, shutil, subprocess
root = Path('/backups/manual')
source = root / 'demo'
target = root / 'damaged'
shutil.copytree(source, target)
metadata = target / 'metadaten.json'
meta = json.loads(metadata.read_text())
meta['repository']['id'] = 'damaged'
metadata.write_text(json.dumps(meta))
mirror = target / 'repository.git'
oid = subprocess.check_output(
    ['git', '-C', str(mirror), 'rev-parse', 'HEAD:hello.txt'], text=True
).strip()
(mirror / 'objects' / oid[:2] / oid[2:]).unlink()
"""
    args = ("--network", "none", "--mount", mount)
    docker("run", "--rm", *args, "--entrypoint", "python", IMAGE, "-c", seed)
    snapshot_code = """
from pathlib import Path
import hashlib
root = Path('/backups')
for p in sorted(root.rglob('*')):
    if p.name not in ('data.json', 'metadaten.json'):
        digest = ''
        if p.is_file():
            with p.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        print(p.relative_to(root), digest)
"""

    def status(container):
        return json.loads(docker("exec", container, "repoharbor", "status", "--json"))

    for enabled in (False, True):
        config = (
            "schema_version: 1\nschedule: '0 0 29 2 *'\n"
            f"features: {{integrity_check: {str(enabled).lower()}}}\n"
            "integrity: {schedule: '* * * * *'}"
        )
        container = docker(
            "run",
            "-d",
            "--read-only",
            *args,
            "-e",
            "REPOHARBOR_TOKEN_SOURCE=env",
            "-e",
            f"REPOHARBOR_CONFIG={config}",
            IMAGE,
        )
        try:
            wait_ready(container)
            no_tcp_listener(container)
            before = status(container)
            assert before["periodic_integrity"]["enabled"] is enabled
            snapshot = docker("exec", container, "python", "-c", snapshot_code)
            deadline = time.monotonic() + 70
            start_minute = int(time.time() // 60)
            while True:
                after = status(container)
                periodic = after["periodic_integrity"]
                if enabled and periodic["state"] == "completed":
                    break
                if not enabled and int(time.time() // 60) > start_minute:
                    assert periodic["state"] == "idle"
                    assert periodic["next_scheduled_at"] is None
                    assert periodic["last_started_at"] is None
                    break
                assert time.monotonic() < deadline, docker("logs", container)
                threading.Event().wait(0.25)
            assert after["last_successful_run_at"] == before["last_successful_run_at"]
            assert docker("exec", container, "python", "-c", snapshot_code) == snapshot
            if enabled:
                assert (
                    periodic["checked"],
                    periodic["healthy"],
                    periodic["corrupt"],
                ) == (2, 1, 1)
                assert periodic["last_result"] == "issues"
                assert "GIT_CORRUPTION" in docker(
                    "exec", container, "repoharbor", "repositories", "show", "damaged"
                )
                docker("restart", "-t", "15", container)
                assert (
                    status(container)["periodic_integrity"]["last_completed_at"]
                    == periodic["last_completed_at"]
                )
                assert (
                    docker("exec", container, "python", "-c", snapshot_code) == snapshot
                )
            docker("stop", "-t", "15", container)
            assert json.loads(docker("inspect", container))[0]["State"]["ExitCode"] == 0
        finally:
            docker("rm", "-f", container)


def logging_smoke():
    yaml = (
        "schema_version: 1\nfeatures: {extended_logging: true}\n"
        "logging: {format: json, file: {enabled: true, "
        "max_size_mib: 1, backup_count: 2}}"
    )
    env = ["-e", "REPOHARBOR_TOKEN_SOURCE=env", "-e", f"REPOHARBOR_CONFIG={yaml}"]
    missing = subprocess.run(
        ["docker", "run", "--rm", *env, IMAGE],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert missing.returncode == 1
    assert json.loads(missing.stderr)["error_code"] == "LOG_MOUNT_MISSING"

    with tempfile.TemporaryDirectory() as directory:
        backups = Path(directory) / "backup"
        backups.mkdir()
        backups.chmod(0o777)
        logs = Path(directory) / "logs"
        logs.mkdir()
        log_mount = f"type=bind,src={logs},dst=/logs"
        backup_mount = f"type=bind,src={backups},dst=/backups"

        def as_owner(code):
            return docker(
                "run",
                "--rm",
                "--mount",
                log_mount,
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                code,
            )

        def fixture_setup(code):
            # Set up and remove the host-side fixture. The service uses its image UID.
            return docker(
                "run",
                "--rm",
                "--user",
                "0",
                "--mount",
                log_mount,
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                code,
            )

        fixture_setup(
            "import os; os.chown('/logs', 10001, 10001); os.chmod('/logs', 0o700)"
        )
        try:
            as_owner(
                "import os; from pathlib import Path; "
                "p = Path('/logs/repoharbor.log'); "
                "p.touch(mode=0o600); p.write_bytes(b'{}\\n'); "
                "os.truncate(p, 1024**2)"
            )
            container = docker(
                "run",
                "-d",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--log-driver",
                "json-file",
                "--log-opt",
                "max-size=10m",
                "--log-opt",
                "max-file=5",
                *env,
                "--mount",
                log_mount,
                "--mount",
                backup_mount,
                IMAGE,
            )
            try:

                def wait_ready_count(expected):
                    deadline = time.monotonic() + 30
                    while docker("logs", container).count('"service.ready"') < expected:
                        assert time.monotonic() < deadline, docker("logs", container)
                        threading.Event().wait(0.1)

                wait_ready_count(1)
                info = json.loads(docker("inspect", container))[0]
                log_config = info["HostConfig"]["LogConfig"]
                assert log_config == {
                    "Type": "json-file",
                    "Config": {
                        "max-file": "5",
                        "max-size": "10m",
                    },
                }
                active = "/logs/repoharbor.log"
                read_log = (
                    "from pathlib import Path; "
                    f"print(Path({active!r}).read_text(), end='')"
                )
                records = [
                    json.loads(line)
                    for line in docker(
                        "exec", container, "python", "-c", read_log
                    ).splitlines()
                ]
                assert [item["event"] for item in records] == [
                    "service.started",
                    "service.ready",
                ]
                assert as_owner(
                    "from pathlib import Path; "
                    "print(Path('/logs/repoharbor.log.1').stat().st_size)"
                ) == str(1024**2)
                inode_code = (
                    f"from pathlib import Path; print(Path({active!r}).stat().st_ino)"
                )
                inode = as_owner(inode_code)
                docker("stop", "-t", "15", container)
                assert '"service.stopped"' in docker("logs", container)
                docker("start", container)
                wait_ready_count(2)
                assert as_owner(inode_code) == inode
                docker("kill", "--signal", "KILL", container)
                docker("start", container)
                wait_ready_count(3)
                assert as_owner(inode_code) == inode
                assert [
                    item["event"]
                    for item in map(json.loads, as_owner(read_log).splitlines())
                ].count("service.ready") == 3
                assert (
                    json.loads(docker("exec", container, "cat", "/backups/data.json"))[
                        "active_operation"
                    ]
                    is None
                )
            finally:
                docker("rm", "-f", container)
        finally:
            fixture_setup(
                "from pathlib import Path; "
                "[p.unlink() for p in Path('/logs').iterdir()]; "
                "Path('/logs').chmod(0o755)"
            )


def smoke():
    assert docker("run", "--rm", IMAGE, "--version") == "0.8.0"
    info = json.loads(docker("image", "inspect", IMAGE))[0]["Config"]
    assert info["User"] == "10001:10001"
    assert not info.get("ExposedPorts") and not info.get("Healthcheck")
    env = [
        "-e",
        "REPOHARBOR_TOKEN_SOURCE=env",
        "-e",
        "REPOHARBOR_CONFIG=schema_version: 1",
    ]
    assert (
        docker("run", "--rm", *env, IMAGE, "config", "validate")
        == "Configuration valid"
    )
    refused = subprocess.run(
        ["docker", "run", "--rm", *env, IMAGE],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert refused.returncode == 1
    assert "dedicated persistent bind mount" in refused.stderr
    with tempfile.TemporaryDirectory() as directory:
        Path(directory).chmod(0o777)
        assert (
            docker(
                "run",
                "--rm",
                *env,
                "--mount",
                f"type=bind,src={directory},dst=/backups",
                IMAGE,
                "config",
                "validate",
            )
            == "Configuration valid"
        )
        assert list(Path(directory).iterdir()) == []
        for init_args in ([], ["--init"]):
            container = docker(
                "run",
                "-d",
                *init_args,
                *env,
                "--mount",
                f"type=bind,src={directory},dst=/backups",
                IMAGE,
            )
            try:
                wait_ready(container)
                no_tcp_listener(container)
                status = json.loads(
                    docker("exec", container, "repoharbor", "status", "--json")
                )
                assert status["service_status"] == "running"
                assert status["last_successful_run_at"] is None
                assert exec_result(container, "backup", "run", "--all").returncode == 4
                docker("stop", "-t", "15", container)
                state = json.loads(docker("inspect", container))[0]["State"]
                assert state["ExitCode"] == 0
                journal = json.loads(
                    docker(
                        "run",
                        "--rm",
                        "--entrypoint",
                        "cat",
                        "--mount",
                        f"type=bind,src={directory},dst=/backups,readonly",
                        IMAGE,
                        "/backups/data.json",
                    )
                )
                assert (
                    journal["active_run"] is None
                    and journal["active_operation"] is None
                )
            finally:
                docker("rm", "-f", container)
        assert (Path(directory) / ".repoharbor.lock").exists()
        manual_smoke(f"type=bind,src={directory},dst=/backups")
    logging_smoke()
    dashboard_smoke(IMAGE)
    print(
        "Docker smoke passed: CLI, non-root, headless, SIGTERM, "
        "manual backup, automatic retry, offline restore, periodic integrity and "
        "log rotation"
    )


if __name__ == "__main__":
    smoke()
