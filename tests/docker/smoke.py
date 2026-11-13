"""Standalone Docker smoke harness; run after building repoharbor:local-test."""

import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

IMAGE = "repoharbor:local-test"


def docker(*args):
    return subprocess.check_output(
        ["docker", *args], text=True, stderr=subprocess.STDOUT, timeout=60
    ).strip()


def smoke():
    assert docker("run", "--rm", IMAGE, "--version") == "0.1.0"
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
                deadline = time.monotonic() + 30
                while "ready;" not in docker("logs", container):
                    state = json.loads(docker("inspect", container))[0]["State"]
                    if not state["Running"] or time.monotonic() >= deadline:
                        raise AssertionError(docker("logs", container))
                    threading.Event().wait(0.1)
                # No TCP listening socket (state 0A), including IPv6.
                check = "from pathlib import Path; " + (
                    "assert not any(line.split()[3]=='0A' "
                    "for p in ('/proc/net/tcp','/proc/net/tcp6') "
                    "for line in Path(p).read_text().splitlines()[1:])"
                )
                docker("exec", container, "python", "-c", check)
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
    print("Docker smoke passed: CLI, mount, non-root, headless, SIGTERM, recreate")


if __name__ == "__main__":
    smoke()
