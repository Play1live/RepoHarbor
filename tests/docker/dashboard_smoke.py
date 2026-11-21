"""Smoke-test the packaged HTTPS UI without exposing a port or using host data."""

import subprocess
import sys
import tempfile
from pathlib import Path

DASHBOARD_SMOKE = """
import http.client
import json
import os
import secrets
import ssl
import subprocess
import time
from importlib.util import find_spec
from pathlib import Path

assert os.getuid() == 10001
assert find_spec("repoharbor.dashboard_demo") is None
cert, key, secret = ("/tmp/test." + name for name in ("crt", "key", "password"))
password = secrets.token_urlsafe(32)
Path(secret).touch(mode=0o600, exist_ok=False)
Path(secret).write_text(password)
subprocess.run(
    ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
     "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
     "-out", cert, "-keyout", key], check=True, capture_output=True,
)
origin = "https://127.0.0.1:8080"
config = {
    "schema_version": 1,
    "schedule": "0 0 1 1 *",
    "features": {"web_ui": True},
    "web": {"origin": origin},
}
env = {
    **os.environ,
    "REPOHARBOR_TOKEN_SOURCE": "env",
    "REPOHARBOR_CONFIG": json.dumps(config),
    "REPOHARBOR_WEB_CERT_FILE": cert,
    "REPOHARBOR_WEB_KEY_FILE": key,
    "REPOHARBOR_WEB_PASSWORD_FILE": secret,
}
context = ssl.create_default_context(cafile=cert)

def exchange(method, path, headers=None, body=None):
    conn = http.client.HTTPSConnection("127.0.0.1", 8080, context=context, timeout=3)
    try:
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        return response.status, response.getheader("Set-Cookie"), response.read()
    finally:
        conn.close()

service = subprocess.Popen(["repoharbor"], env=env)
try:
    deadline = time.monotonic() + 10
    while True:
        try:
            assert exchange("GET", "/api/dashboard")[0] == 401
            break
        except ConnectionRefusedError:
            assert service.poll() is None and time.monotonic() < deadline
            time.sleep(.1)
    code, cookie, _ = exchange(
        "POST", "/api/login", {"Origin": origin, "Content-Type": "application/json"},
        json.dumps({"password": password}),
    )
    assert code == 200 and "Secure" in cookie and "HttpOnly" in cookie
    auth = {"Cookie": cookie.partition(";")[0]}
    code, _, content = exchange("GET", "/api/dashboard", auth)
    data = json.loads(content)
    assert code == 200 and "demo" not in data
    assert data["service"]["service_status"] == "running"
    assert data["repositories"] == [] and data["settings"]["manual_run"] is False
    assert exchange("GET", "/api/dashboard?scenario=large", auth)[0] == 400
    assert exchange("GET", "/api/debug", auth)[0] == 404
    assert exchange("GET", "/assets/brand.svg")[0] == 200
finally:
    service.terminate()
    assert service.wait(5) == 0
"""


def dashboard_smoke(image):
    with tempfile.TemporaryDirectory() as root:
        Path(root).chmod(0o777)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--mount",
                f"type=bind,src={root},dst=/backups",
                "--tmpfs",
                "/tmp:rw,uid=10001,gid=10001,mode=0700",
                "--entrypoint",
                "python",
                image,
                "-c",
                DASHBOARD_SMOKE,
            ],
            check=True,
            timeout=45,
        )
    print("Packaged HTTPS dashboard smoke passed.")


if __name__ == "__main__":
    dashboard_smoke(sys.argv[1] if len(sys.argv) > 1 else "repoharbor:local-test")
