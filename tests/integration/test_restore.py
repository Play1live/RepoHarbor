import errno
import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import git, raw_project, raw_repo

from repoharbor.catalog import directory
from repoharbor.cli import main
from repoharbor.errors import Result
from repoharbor.github import parse_repository
from repoharbor.gitlab import parse_project
from repoharbor.persistence import Store, atomic_json, metadata, read_json
from repoharbor.providers import Observation
from repoharbor.registry import plan


@pytest.fixture
def local_inventory(tmp_path, remote, monkeypatch):
    root = tmp_path / "backup"
    root.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    config = (
        "schema_version: 1\nfeatures: {restore_helper: true}\ncredentials: [unused]\n"
    )
    monkeypatch.setenv("REPOHARBOR_TOKEN_SOURCE", "file")
    monkeypatch.setenv("REPOHARBOR_CONFIG", config)
    monkeypatch.setenv("REPOHARBOR_BACKUP_ROOT", str(root))
    monkeypatch.setenv("REPOHARBOR_SECRETS_DIR", str(secrets))
    monkeypatch.setattr("repoharbor.cli.verify_mount", lambda root: None)
    work, _ = remote
    git("branch", "main-base", cwd=work)
    git("switch", "-c", "dev/topic", cwd=work)
    (work / "hello.txt").write_text("second\n")
    git("commit", "-am", "second", cwd=work)
    git("tag", "v1", "main", cwd=work)
    git("tag", "-am", "annotated", "v2", cwd=work)
    git("update-ref", "refs/merge-requests/17/head", "HEAD", cwd=work)
    blob = git("rev-parse", "HEAD:hello.txt", cwd=work)
    git("update-ref", "refs/custom/file", blob, cwd=work)
    git("notes", "add", "-m", "note", cwd=work)
    git("switch", "main", cwd=work)

    def make(key="demo", *, empty=False):
        folder = directory(root, key)
        folder.mkdir(parents=True)
        mirror = folder / "repository.git"
        if empty:
            git("init", "--bare", "--initial-branch=unborn", mirror)
        else:
            git("clone", "--mirror", "--no-hardlinks", work, mirror)
            git("symbolic-ref", "refs/custom/symbolic", "refs/heads/main", cwd=mirror)
        if ":" in key:
            from repoharbor.config import Config

            kind, host, rid = key.split(":", 2)
            repo = (
                parse_repository(raw_repo(rid=int(rid)))
                if kind == "github"
                else parse_project(raw_project(rid=int(rid), host=host), host)
            )
            proposed, _ = plan(
                Config((), (), "file"),
                {},
                [Observation(repo, "unused", False)],
                set(),
                provider_type=kind,
                host=host,
            )
            meta = proposed[key]
        else:
            meta = metadata(key, "https://example.org/repo.git", None, "unused")
        meta["source"].update(status="unavailable", discovery_status="failed")
        meta["selection"].update(status="excluded", reason="removed_from_configuration")
        atomic_json(folder / "metadaten.json", meta)
        return mirror

    with Store(root):
        pass
    return root, secrets, work, make


def invoke(monkeypatch, mode, value, repository="demo", *options):
    option = "--destination" if mode == "clone" else "--output"
    monkeypatch.setattr(
        "sys.argv",
        [
            "repoharbor",
            "restore",
            mode,
            "--repository",
            repository,
            option,
            str(value),
            *options,
        ],
    )
    return main()


def snapshot(root):
    return {
        p.relative_to(root): (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


def assert_same_repository(mirror, restored, branch):
    assert git("rev-parse", "--is-bare-repository", cwd=restored) == "false"
    assert git("symbolic-ref", "HEAD", cwd=restored) == f"refs/heads/{branch}"
    assert git("rev-parse", "HEAD", cwd=restored) == git(
        "rev-parse", branch, cwd=mirror
    )
    assert git("log", "--all", "--format=%H", cwd=restored) == git(
        "log", "--all", "--format=%H", cwd=mirror
    )
    assert git("for-each-ref", cwd=restored) == git("for-each-ref", cwd=mirror)
    assert (restored / "hello.txt").read_text() == git(
        "show", f"{branch}:hello.txt", cwd=mirror
    ) + "\n"
    assert not (restored / ".git/objects/info/alternates").exists()


@pytest.mark.parametrize(
    ("key", "branch"),
    [
        ("demo", None),
        ("github:github.com:17", "dev/topic"),
        ("gitlab:gitlab.com:17", None),
        ("gitlab:gitlab.example.org:18", "dev/topic"),
    ],
)
def test_offline_clone_and_bundle(local_inventory, monkeypatch, tmp_path, key, branch):
    root, _, _, make = local_inventory
    mirror = make(key)
    # These settings would be unsafe or alter results if inherited by the reader.
    git("config", "core.hooksPath", "/nonexistent", cwd=mirror)
    git("config", "remote.origin.promisor", "true", cwd=mirror)
    before = snapshot(root)
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "one")
    monkeypatch.setattr(
        "repoharbor.cli.Credentials.resolve",
        lambda *args: pytest.fail("restore resolved credentials"),
    )
    monkeypatch.setattr(
        "repoharbor.cli.request",
        lambda *args: pytest.fail("restore contacted service"),
    )
    target, output = tmp_path / "restored", tmp_path / "export.bundle"
    options = ("--branch", branch) if branch else ()
    assert invoke(monkeypatch, "clone", target, key, *options) == 0
    assert invoke(monkeypatch, "bundle", output, key) == 0
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    assert_same_repository(mirror, target, branch or "main")
    assert git("symbolic-ref", "refs/custom/symbolic", cwd=target) == "refs/heads/main"
    for p in (target / ".git/objects").rglob("*"):
        if p.is_file():
            assert p.stat().st_nlink == 1
    assert snapshot(root) == before
    original_refs = git("for-each-ref", cwd=mirror)
    original_commit = git("rev-parse", "HEAD", cwd=mirror)
    shutil.rmtree(root)
    from_bundle = tmp_path / "from-bundle"
    git("clone", "--mirror", output, from_bundle)
    assert git("for-each-ref", cwd=from_bundle) == original_refs
    assert git("rev-parse", "HEAD", cwd=from_bundle) == original_commit
    git("fsck", "--full", cwd=from_bundle)
    working_tree = tmp_path / "work-from-bundle"
    git("clone", output, working_tree)
    assert (working_tree / "hello.txt").read_text() == "first\n"
    assert git("rev-parse", "HEAD", cwd=working_tree) == original_commit


def test_empty_and_missing_default(local_inventory, monkeypatch, tmp_path):
    _, _, _, make = local_inventory
    mirror = make(empty=True)
    target, bundle = tmp_path / "empty", tmp_path / "empty.bundle"
    assert invoke(monkeypatch, "clone", target) == 0
    assert git("symbolic-ref", "HEAD", cwd=target) == "refs/heads/unborn"
    assert set(p.name for p in target.iterdir()) == {".git"}
    assert invoke(monkeypatch, "bundle", bundle) == Result.RESTORE_FAILED
    assert not bundle.exists()
    mirror = make("missing-head")
    git("symbolic-ref", "HEAD", "refs/heads/missing", cwd=mirror)
    fallback = tmp_path / "fallback"
    assert invoke(monkeypatch, "clone", fallback, "missing-head") == 0
    assert_same_repository(mirror, fallback, "dev/topic")
    assert invoke(monkeypatch, "bundle", bundle, "missing-head") == 0
    assert not any(
        line.endswith(" HEAD")
        for line in git("bundle", "list-heads", bundle).splitlines()
    )


def test_detached_head_and_unusual_refs(local_inventory, monkeypatch, tmp_path):
    _, _, _, make = local_inventory
    mirror = make()
    detached = git("rev-parse", "dev/topic", cwd=mirror)
    git("update-ref", "--no-deref", "HEAD", detached, cwd=mirror)
    git("update-ref", "refs/custom/☃", detached, cwd=mirror)
    odd_ref = os.fsdecode(b"refs/custom/byte-\xff")
    git("update-ref", odd_ref, detached, cwd=mirror)
    target, bundle = tmp_path / "detached", tmp_path / "detached.bundle"
    assert invoke(monkeypatch, "clone", target) == 0
    assert invoke(monkeypatch, "bundle", bundle) == 0
    assert (target / ".git/HEAD").read_text().strip() == detached
    assert git("rev-parse", "refs/custom/☃", cwd=target) == detached
    assert git("rev-parse", odd_ref, cwd=target) == detached
    assert (target / "hello.txt").read_text() == "second\n"


@pytest.mark.parametrize("mode", ["clone", "bundle"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "corrupt",
        "nonbare",
        "wrong-id",
        "wrong-path",
        "journal",
        "partial",
        "lock",
        "alternate",
        "shallow",
        "promisor",
    ],
)
def test_unsafe_mirror_state(
    local_inventory, monkeypatch, tmp_path, mode, fault, capsys
):
    root, _, _, make = local_inventory
    mirror = make()
    if fault == "missing":
        mirror.rename(mirror.with_suffix(".old"))
    elif fault == "corrupt":
        oid = git("rev-parse", "HEAD:hello.txt", cwd=mirror)
        path = mirror / "objects" / oid[:2] / oid[2:]
        path.write_bytes(b"corrupt")
    elif fault == "nonbare":
        git("config", "core.bare", "false", cwd=mirror)
    elif fault in ("wrong-id", "wrong-path"):
        path = mirror.parent / "metadaten.json"
        meta = read_json(path)
        if fault == "wrong-id":
            meta["repository"]["id"] = "other"
        else:
            meta["backup"]["relative_path"] = "../other"
        atomic_json(path, meta)
    elif fault == "journal":
        with Store(root) as store:
            store.begin("demo", "mirror_update")
    elif fault == "partial":
        mirror.with_suffix(".git.partial").mkdir()
    elif fault == "alternate":
        (mirror / "objects/info/alternates").write_text("/other/objects\n")
    elif fault == "shallow":
        (mirror / "shallow").touch()
    elif fault == "promisor":
        (mirror / "objects/pack/other.promisor").touch()
    else:
        (mirror / "packed-refs.lock").touch()
    before = snapshot(root)
    target = tmp_path / "result"
    assert invoke(monkeypatch, mode, target) == Result.RESTORE_FAILED
    assert not target.exists()
    assert not capsys.readouterr().out
    assert snapshot(root) == before


def test_rejects_unknown_invalid_and_busy(local_inventory, monkeypatch, tmp_path):
    root, _, _, make = local_inventory
    make()
    target = tmp_path / "result"
    assert (
        invoke(monkeypatch, "clone", target, "../../secret") == Result.INVALID_ARGUMENT
    )
    assert invoke(monkeypatch, "clone", target, "unknown") == Result.UNKNOWN_REPOSITORY
    assert (
        invoke(monkeypatch, "clone", target, "demo", "--branch", "missing")
        == Result.INVALID_ARGUMENT
    )
    assert (
        invoke(monkeypatch, "clone", target, "demo", "--branch", "main..dev")
        == Result.INVALID_ARGUMENT
    )
    with Store(root):
        assert invoke(monkeypatch, "clone", target) == Result.BUSY
        assert invoke(monkeypatch, "bundle", target) == Result.BUSY
    assert not target.exists()


def test_rejects_protected_paths_symlinks_and_existing_targets(
    local_inventory, monkeypatch, tmp_path
):
    root, secrets, _, make = local_inventory
    mirror = make()
    existing = tmp_path / "existing"
    existing.mkdir()
    existing_file = tmp_path / "existing.bundle"
    existing_file.write_bytes(b"user data")
    escape = tmp_path / "escape"
    escape.symlink_to(root, target_is_directory=True)
    dst_link = tmp_path / "dangling"
    dst_link.symlink_to(tmp_path / "missing")
    before = snapshot(root)
    for mode in ("clone", "bundle"):
        for protected in (
            root / "result",
            secrets / "result",
            mirror / "result",
            existing / "../result",
            "",
        ):
            assert invoke(monkeypatch, mode, protected) == Result.INVALID_ARGUMENT
        for path in (
            existing,
            existing_file,
            escape / "result",
            dst_link,
            tmp_path / "missing" / "result",
        ):
            assert invoke(monkeypatch, mode, path) == Result.DESTINATION_FAILED
    assert list(existing.iterdir()) == []
    assert existing_file.read_bytes() == b"user data"
    assert dst_link.is_symlink()
    assert snapshot(root) == before


@pytest.mark.parametrize("mode", ["clone", "bundle"])
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO, errno.EACCES, errno.EEXIST])
def test_atomic_publication_failure(
    local_inventory, monkeypatch, tmp_path, mode, code, capsys
):
    root, _, _, make = local_inventory
    make()
    before = snapshot(root)
    target = tmp_path / "result"
    from repoharbor import restore

    publish = restore.publish

    def fail(source, parent_fd, name):
        if code == errno.EEXIST:
            target.mkdir()
            publish(source, parent_fd, name)
        raise OSError(code, "private path not for output")

    monkeypatch.setattr(restore, "publish", fail)
    assert invoke(monkeypatch, mode, target) == Result.DESTINATION_FAILED
    assert target.exists() == (code == errno.EEXIST)
    if target.exists():
        assert list(target.iterdir()) == []
    assert not list(tmp_path.glob(".repoharbor-restore-incomplete-*"))
    assert not capsys.readouterr().out
    assert snapshot(root) == before


@pytest.mark.parametrize("mode", ["clone", "bundle"])
def test_feature_gate_and_manual_run_are_independent(
    local_inventory, monkeypatch, tmp_path, mode
):
    _, _, _, make = local_inventory
    make()
    output = tmp_path / "result"
    monkeypatch.setenv(
        "REPOHARBOR_CONFIG",
        "schema_version: 1\nfeatures: {manual_run: true, restore_helper: false}",
    )
    assert invoke(monkeypatch, mode, output) == Result.FEATURE_DISABLED
    assert not output.exists()
    monkeypatch.setenv(
        "REPOHARBOR_CONFIG",
        "schema_version: 1\nfeatures: {manual_run: false, restore_helper: true}",
    )
    assert invoke(monkeypatch, mode, output) == Result.SUCCESS
    monkeypatch.setattr("sys.argv", ["repoharbor", "backup", "run", "--all"])
    assert main() == Result.FEATURE_DISABLED


@pytest.mark.parametrize("bad_lock", ["missing", "directory", "symlink", "fifo"])
def test_requires_existing_regular_lock(
    local_inventory, monkeypatch, tmp_path, bad_lock
):
    root, _, _, make = local_inventory
    make()
    lock = root / ".repoharbor.lock"
    lock.unlink()
    if bad_lock == "directory":
        lock.mkdir()
    elif bad_lock == "symlink":
        lock.symlink_to(root / "data.json")
    elif bad_lock == "fifo":
        os.mkfifo(lock)
    assert invoke(monkeypatch, "clone", tmp_path / "result") == Result.RESTORE_FAILED
    assert not (tmp_path / "result").exists()
    assert lock.exists() == (bad_lock != "missing")


def test_read_only_restore_refuses_symlink_mirror(
    local_inventory, monkeypatch, tmp_path
):
    _, _, _, make = local_inventory
    mirror = make()
    moved = mirror.rename(tmp_path / "moved")
    mirror.symlink_to(moved, target_is_directory=True)
    assert invoke(monkeypatch, "bundle", tmp_path / "bundle") == Result.RESTORE_FAILED


def test_symlink_swap_at_publication(local_inventory, monkeypatch, tmp_path):
    root, _, _, make = local_inventory
    make()
    from repoharbor import restore

    target = tmp_path / "raced.bundle"
    before = snapshot(root)
    publish = restore.publish

    def race(source, parent_fd, name):
        target.symlink_to(root / "data.json")
        publish(source, parent_fd, name)

    monkeypatch.setattr(restore, "publish", race)
    assert invoke(monkeypatch, "bundle", target) == Result.DESTINATION_FAILED
    assert target.is_symlink()
    assert snapshot(root) == before


def test_renamed_destination_parent_is_detected(local_inventory, monkeypatch, tmp_path):
    root, _, _, make = local_inventory
    make()
    from repoharbor import restore

    parent = tmp_path / "output"
    parent.mkdir()
    before = snapshot(root)
    transfer = restore.transfer

    def move(*args):
        transfer(*args)
        parent.rename(tmp_path / "moved")
        parent.symlink_to(root, target_is_directory=True)

    monkeypatch.setattr(restore, "transfer", move)
    assert invoke(monkeypatch, "bundle", parent / "bundle") == Result.DESTINATION_FAILED
    assert not list((tmp_path / "moved").iterdir())
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "fault", ["requires-history", "no-ref", "truncated-pack", "offline-ref"]
)
def test_rejects_incomplete_generated_bundles(
    local_inventory, monkeypatch, tmp_path, fault
):
    _, _, _, make = local_inventory
    make()
    from repoharbor import restore

    run = restore.git_at

    def broken_export(git_runner, repo, *args):
        if fault == "requires-history" and args[:2] == ("bundle", "create"):
            return run(git_runner, repo, *args, "^refs/heads/main")
        output = run(git_runner, repo, *args)
        if fault == "no-ref" and args[:2] == ("bundle", "list-heads"):
            return output.split("\n", 1)[1]
        if fault == "truncated-pack" and args[:2] == ("bundle", "create"):
            bundle = Path(args[2])
            bundle.write_bytes(bundle.read_bytes()[:-32])
        if fault == "offline-ref" and "fetch" in args:
            run(git_runner, repo, "update-ref", "-d", "refs/tags/v1")
        return output

    monkeypatch.setattr(restore, "git_at", broken_export)
    output = tmp_path / "bundle"
    assert invoke(monkeypatch, "bundle", output) == Result.RESTORE_FAILED
    assert not output.exists()


def test_sha256_repository_and_bundle(local_inventory, monkeypatch, tmp_path):
    _, _, _, make = local_inventory
    mirror = make(empty=True)
    shutil.rmtree(mirror)
    work = tmp_path / "sha256"
    git("init", "--object-format=sha256", "-b", "main", work)
    (work / "hello.txt").write_text("sha256\n")
    git("add", "hello.txt", cwd=work)
    git(
        "-c",
        "user.name=Example",
        "-c",
        "user.email=test@example.org",
        "commit",
        "-m",
        "fixture",
        cwd=work,
    )
    git("tag", "v1", cwd=work)
    git("clone", "--mirror", work, mirror)
    restored, bundle = tmp_path / "result", tmp_path / "export"
    assert invoke(monkeypatch, "clone", restored) == Result.SUCCESS
    assert invoke(monkeypatch, "bundle", bundle) == Result.SUCCESS
    assert_same_repository(mirror, restored, "main")
    verifier = tmp_path / "verifier"
    git("init", "--object-format=sha256", verifier)
    assert "complete history" in git("bundle", "verify", bundle, cwd=verifier)


def test_bundle_events_and_failed_retry_preserve_offline_state(
    local_inventory, log_mount, monkeypatch, tmp_path, capsys
):
    root, _, _, make = local_inventory
    make()
    before = snapshot(root)
    logfile = log_mount / "restore.log"
    monkeypatch.setenv(
        "REPOHARBOR_CONFIG",
        "schema_version: 1\nfeatures: {restore_helper: true, extended_logging: true}\n"
        "logging:\n  format: json\n  level: DEBUG\n"
        f"  file: {{enabled: true, path: {logfile}}}",
    )
    output = tmp_path / "example.bundle"
    assert invoke(monkeypatch, "bundle", output) == Result.SUCCESS
    assert invoke(monkeypatch, "bundle", output) == Result.DESTINATION_FAILED
    assert (
        invoke(monkeypatch, "bundle", output, "fictional-credential-in-user-input")
        == Result.UNKNOWN_REPOSITORY
    )
    assert snapshot(root) == before
    captured = capsys.readouterr()
    raw = logfile.read_text()
    assert raw == captured.err
    assert "fictional-credential-in-user-input" not in raw + captured.out
    assert str(output) not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert [event["event"] for event in events] == [
        "restore.started",
        "restore.bundle_created",
        "restore.bundle_verified",
        "restore.completed",
        "restore.started",
        "restore.failed",
        "restore.failed",
    ]
    assert len({event["run_id"] for event in events[:4]}) == 1
    assert events[-1]["result"] == "UNKNOWN_REPOSITORY"
