import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from repoharbor.errors import Result
from repoharbor.persistence import Store


@pytest.mark.parametrize(
    ("mode", "command"), [("bundle", "create"), ("clone", "checkout")]
)
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_interrupted_restore_holds_lock_and_never_publishes(
    factory, tmp_path, mode, command, sig
):
    with factory() as engine:
        assert engine.run() == Result.SUCCESS
    root = factory.root
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    git = shutil.which("git")
    assert git
    marker, release = tmp_path / "checkpoint", tmp_path / "release"
    shim = tmp_path / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"status = subprocess.call([{git!r}, *sys.argv[1:]], close_fds=False)\n"
        f"if {command!r} in sys.argv and status == 0:\n"
        f"    Path({str(marker)!r}).touch()\n"
        f"    while not Path({str(release)!r}).is_file():\n"
        "        time.sleep(0.01)\n"
        "sys.exit(status)\n"
    )
    shim.chmod(0o700)
    env = {
        **os.environ,
        "REPOHARBOR_TOKEN_SOURCE": "file",
        "REPOHARBOR_BACKUP_ROOT": str(root),
        "REPOHARBOR_CONFIG": "schema_version: 1\nfeatures: {restore_helper: true}",
    }
    target = tmp_path / "restore-result"
    command_line = [
        sys.executable,
        "-c",
        "from repoharbor import cli; cli.verify_mount = lambda root: None; "
        "raise SystemExit(cli.main())",
        "restore",
        mode,
        "--repository",
        "demo",
        "--destination" if mode == "clone" else "--output",
    ]
    process = subprocess.Popen(
        [*command_line, str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**env, "PATH": f"{tmp_path}:{env['PATH']}"},
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with pytest.raises(BlockingIOError), Store(root):
            pytest.fail("Restore child did not take the Store lock")
        competing = subprocess.run(
            [*command_line, str(tmp_path / "competing")],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert competing.returncode == Result.BUSY
        process.send_signal(sig)
        out, err = process.communicate(timeout=10)
        assert not out and not target.exists()
        stages = list(tmp_path.glob(".repoharbor-restore-incomplete-*"))
        if sig == signal.SIGKILL:
            assert process.returncode == -signal.SIGKILL
            assert len(stages) == 1
            # The git process still has the same open lock description after
            # its parent dies. A backup cannot race with its remaining reads.
            with pytest.raises(BlockingIOError), Store(root):
                pytest.fail("A surviving Git process lost its lock")
        else:
            assert process.returncode == Result.RESTORE_FAILED
            assert "Restore interrupted" in err
            assert stages == []
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    ready = time.monotonic() + 5
    while True:
        try:
            with Store(root):
                break
        except BlockingIOError:
            assert time.monotonic() < ready
            time.sleep(0.01)
    retry = tmp_path / "retry-result"
    completed = subprocess.run(
        [*command_line, str(retry)], env=env, capture_output=True, text=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr
    assert retry.exists()
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(("mode", "at"), [("bundle", "create"), ("clone", "checkout")])
@pytest.mark.parametrize("error", ["No space left on device", "Input/output error"])
def test_git_output_write_error(
    factory, tmp_path, monkeypatch, mode, at, error, capsys
):
    from repoharbor.cli import main

    with factory() as engine:
        assert engine.run() == Result.SUCCESS
    root = factory.root
    target = tmp_path / "failed"
    git = shutil.which("git")
    assert git
    shim = tmp_path / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        f"if {at!r} in sys.argv:\n"
        "    stage = Path(sys.argv[sys.argv.index('-C') + 1]).parent\n"
        "    (stage / 'incomplete-output.lock').write_bytes(b'short write')\n"
        f"    print({error!r}, file=sys.stderr)\n"
        "    sys.exit(1)\n"
        f"os.execv({git!r}, [{git!r}, *sys.argv[1:]])\n"
    )
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("REPOHARBOR_TOKEN_SOURCE", "file")
    monkeypatch.setenv(
        "REPOHARBOR_CONFIG", "schema_version: 1\nfeatures: {restore_helper: true}"
    )
    monkeypatch.setenv("REPOHARBOR_BACKUP_ROOT", str(root))
    monkeypatch.setattr("repoharbor.cli.verify_mount", lambda root: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repoharbor",
            "restore",
            mode,
            "--repository",
            "demo",
            "--destination" if mode == "clone" else "--output",
            str(target),
        ],
    )
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert main() == Result.RESTORE_FAILED
    assert not capsys.readouterr().out
    assert not target.exists()
    assert not list(tmp_path.glob(".repoharbor-restore-incomplete-*"))
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
