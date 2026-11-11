from dataclasses import replace

import pytest
from conftest import git

from repoharbor.errors import HarborError
from repoharbor.persistence import read_json
from repoharbor.recovery import recover


def test_mirror_lifecycle_and_offline_restore(factory, remote, tmp_path):
    work, bare = remote
    with factory() as engine:
        recover(engine)
        engine.run()
        folder = engine.path("demo")
        mirror = folder / "repository.git"
        assert git("rev-parse", "--is-bare-repository", cwd=mirror) == "true"
        assert not (mirror / "hello.txt").exists()
        assert engine.repos["demo"]["backup"]["successful_updates"] == 1
        first = engine.repos["demo"]["backup"].copy()
        calls = len(engine.git.calls)
        engine.run()
        assert not any("fetch" in args for args in engine.git.calls[calls:])
        assert (
            engine.repos["demo"]["backup"]["last_updated_at"]
            == first["last_updated_at"]
        )
        git("checkout", "-b", "topic", cwd=work)
        (work / "hello.txt").write_text("changed\n")
        git("commit", "-am", "change", cwd=work)
        git("tag", "v-test", cwd=work)
        git("push", "--all", cwd=work)
        git("push", "--tags", cwd=work)
        engine.run()
        assert "refs/heads/topic" in engine.git.refs(mirror)
        assert "refs/tags/v-test" in engine.git.refs(mirror)
        git("push", "origin", "--delete", "topic", "v-test", cwd=work)
        engine.run()
        assert "topic" not in engine.git.refs(
            mirror
        ) and "v-test" not in engine.git.refs(mirror)
        # Force-push main to a different history tip.
        git("push", "--force", "origin", "HEAD:main", cwd=work)
        engine.run()
        assert git("rev-parse", "refs/heads/main", cwd=mirror) == git(
            "rev-parse", "HEAD", cwd=work
        )
        restored = tmp_path / "restored"
        git("clone", "--no-hardlinks", mirror, restored)
        assert (restored / "hello.txt").read_text() == "changed\n"
        assert len(git("log", "--oneline", cwd=restored).splitlines()) == 2
        # Config and metadata are secret-free and outside the mirror.
        assert (folder / "metadaten.json").exists()
        assert not (mirror / "metadaten.json").exists()
        assert "https://" not in (factory.root / "data.json").read_text()
        assert not (folder / "repository.git.partial").exists()
        assert read_json(folder / "metadaten.json")["source"]["status"] == "available"


def test_exclude_resume_outage_preserves_mirror(factory, remote, tmp_path):
    with factory() as engine:
        engine.run()
        path = engine.path("demo") / "repository.git"
        inode = path.stat().st_ino
        updated = engine.repos["demo"]["backup"]["last_updated_at"]
    with factory(replace(factory.config, sources=())) as engine:
        recover(engine)
        engine.run()
        assert engine.repos["demo"]["selection"]["status"] == "excluded"
        assert engine.git.calls == []
        assert path.stat().st_ino == inode
    with factory() as engine:
        recover(engine)
        engine.run()
        assert engine.repos["demo"]["selection"]["status"] == "included"
        assert engine.repos["demo"]["backup"]["last_updated_at"] == updated
        remote[1].rename(tmp_path / "offline.git")
        engine.run()
        assert path.stat().st_ino == inode
        assert engine.repos["demo"]["source"]["status"] == "unavailable"
        assert engine.repos["demo"]["backup"]["last_updated_at"] == updated
        git("clone", "--no-hardlinks", path, tmp_path / "offline-restore")
        assert (tmp_path / "offline-restore" / "hello.txt").exists()


def test_empty_remote_and_extra_refs(factory, remote):
    git("update-ref", "-d", "refs/heads/main", cwd=remote[1])
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        assert engine.git.healthy(mirror)
        assert engine.git.refs(mirror) == ""
        oid = git("rev-parse", "HEAD", cwd=remote[0])
        git("update-ref", "refs/custom/a", oid, cwd=remote[1])
        engine.run()
        assert "refs/custom/a" in engine.git.refs(mirror)


def test_fetch_failure_and_dangling_objects(factory, remote, monkeypatch):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        git("hash-object", "-w", remote[0] / "hello.txt", cwd=mirror)
        assert engine.git.healthy(mirror)
        old = engine.git.refs(mirror)
        original = engine.git.fetch

        def fail(*args, **kwargs):
            raise HarborError("GIT_FAILED")

        monkeypatch.setattr(engine.git, "fetch", fail)
        git("update-ref", "refs/heads/new", "refs/heads/main", cwd=remote[1])
        engine.run()
        assert engine.git.refs(mirror) == old
        assert engine.repos["demo"]["backup"]["status"] == "error"
        assert engine.repos["demo"]["integrity"]["status"] == "suspect"
        monkeypatch.setattr(engine.git, "fetch", original)
        recover(engine)
        assert engine.repos["demo"]["integrity"]["status"] == "healthy"
        engine.run()
        assert "refs/heads/new" in engine.git.refs(mirror)


def test_identity_conflicts_fail_closed(factory):
    from repoharbor.config import Source

    with factory() as engine:
        engine.run()
    for source in (
        Source("demo", "https://example.org/other.git"),
        Source("other", "https://example.org/repo.git"),
    ):
        with (
            pytest.raises(HarborError),
            factory(replace(factory.config, sources=(source,))),
        ):
            pass


def test_default_branch_change_and_annotated_tag(factory, remote, tmp_path):
    with factory() as engine:
        engine.run()
        mirror = engine.path("demo") / "repository.git"
        git("update-ref", "refs/heads/next", "HEAD", cwd=remote[1])
        git("symbolic-ref", "HEAD", "refs/heads/next", cwd=remote[1])
        git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=test@example.org",
            "tag",
            "-am",
            "annotation",
            "annotated",
            cwd=remote[1],
        )
        engine.run()
        assert engine.git.head(mirror) == "refs/heads/next"
        updates = engine.repos["demo"]["backup"]["successful_updates"]
        engine.run()
        assert engine.repos["demo"]["backup"]["successful_updates"] == updates
        git("clone", "--no-hardlinks", mirror, tmp_path / "next-restore")
        assert (tmp_path / "next-restore" / "hello.txt").exists()
        # HEAD-only changes also alter the recoverable default checkout.
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote[1])
        engine.run()
        assert engine.git.head(mirror) == "refs/heads/main"
        assert engine.repos["demo"]["backup"]["successful_updates"] == updates + 1


def test_remote_race_fetch_without_effect_does_not_claim_update(factory, monkeypatch):
    with factory() as engine:
        engine.run()
        old = engine.repos["demo"]["backup"].copy()
        real = engine.git.remote_refs

        def raced(*args):
            return real(*args) + "\nfake-observation refs/heads/gone"

        monkeypatch.setattr(engine.git, "remote_refs", raced)
        engine.run()
        assert (
            engine.repos["demo"]["backup"]["last_updated_at"] == old["last_updated_at"]
        )
        assert (
            engine.repos["demo"]["backup"]["successful_updates"]
            == old["successful_updates"]
        )
        assert engine.repos["demo"]["backup"]["status"] == "up_to_date"
