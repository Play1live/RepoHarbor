import copy
import io
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
from conftest import api_response, raw_repo

from repoharbor import cli
from repoharbor.config import Discovery, Source
from repoharbor.github import GitHub
from repoharbor.persistence import read_json


def broken_response():
    response = Mock(closed=False)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.side_effect = IncompleteRead(b"fictional-secret response body")
    return response


@pytest.mark.parametrize("status", [200, 403])
def test_protocol_fault_preserves_backups_and_service_runs_next_schedule(
    github_env, monkeypatch, caplog, status
):
    env = github_env
    cfg = replace(
        env.config,
        sources=(Source("demo", "https://example.org/repo.git"),),
        providers=env.config.providers[:1],
        discovery=Discovery(0),
        schedule="* * * * *",
    )
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        before = copy.deepcopy(engine.repos[env.key])
        metadata_path = engine.path(env.key) / "metadaten.json"
        metadata_bytes = metadata_path.read_bytes()
        mirror = engine.path(env.key) / "repository.git"
        inode = mirror.stat().st_ino
        manual_checks = engine.repos["demo"]["backup"]["last_checked_at"]
        failure = broken_response()
        if status == 403:
            failure = HTTPError("", 403, "fictional-secret", {}, failure)
        link = '<https://api.github.com/user/repos?per_page=100&page=2>; rel="next"'
        opener = Mock()
        # The interrupted second page must not publish even a valid new first page.
        opener.open.side_effect = [
            api_response({"login": "fictional"}),
            api_response([raw_repo(), raw_repo(18, name="new")], {"Link": link}),
            failure,
            api_response({"login": "fictional"}),
            api_response([raw_repo()]),
        ]
        monkeypatch.setattr("repoharbor.engine.GitHub", GitHub)
        monkeypatch.setattr("repoharbor.github.build_opener", lambda *args: opener)
        completed = threading.Event()
        stop = engine.git.stop
        stop_wait = stop.wait
        runs = []
        original = engine.run

        def run():
            try:
                original()
                runs.append(engine.store.data["discovery"]["state"])
                if len(runs) == 1:
                    assert engine.sources == cfg.sources
                    assert engine.repos[env.key] == before
                    assert metadata_path.read_bytes() == metadata_bytes
                    assert "github:github.com:18" not in engine.repos
                    assert (
                        engine.repos["demo"]["backup"]["last_checked_at"]
                        != manual_checks
                    )
                    assert engine.store.data["active_run"]["state"] == "completed"
                    assert not stop.is_set()
                else:
                    stop.set()
            finally:
                completed.set()

        class Clock:
            count = 0

            @classmethod
            def now(cls, zone):
                cls.count += 1
                return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=cls.count)

        def wait(timeout):
            if timeout != 0.25:
                return stop_wait(timeout)
            assert completed.wait(5)
            completed.clear()

        monkeypatch.setattr(engine, "run", run)
        monkeypatch.setattr(cli, "datetime", Clock)
        monkeypatch.setattr(stop, "wait", wait)
        cli.serve(engine, stop)
        assert runs == ["failed", "completed"]
        assert opener.open.call_count == 5
        stop.clear()
        assert mirror.stat().st_ino == inode and engine.git.healthy(mirror)
        assert "fictional-secret" not in caplog.text
        assert "response body" not in caplog.text
        assert "GITHUB_INVALID_RESPONSE_OR_NETWORK" in caplog.text


@pytest.mark.parametrize("delay", [0, 1000, 5000])
def test_server_cooldown_persisted_across_restart_beyond_one_day(
    github_env, monkeypatch, github_clock, delay
):
    env = github_env
    cfg = replace(
        env.config,
        sources=(Source("demo", "https://example.org/repo.git"),),
        providers=env.config.providers[:1],
        discovery=Discovery(delay),
    )
    until = github_clock.time() + 172800
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.run()
        before = copy.deepcopy(engine.repos[env.key])
        monkeypatch.setattr("repoharbor.engine.GitHub", GitHub)
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "",
            429,
            "",
            {"retry-after": "120", "x-ratelimit-reset": str(until)},
            io.BytesIO(),
        )
        monkeypatch.setattr("repoharbor.github.build_opener", lambda *args: opener)
        monkeypatch.setattr("time.time", github_clock.time)
        engine.run()
        assert engine.store.data["discovery"]["retry_at"] == until
        assert engine.sources == cfg.sources
        assert engine.repos[env.key] == before
        assert read_json(cfg.root / "data.json")["discovery"]["retry_at"] == until
    # Even after the old one-day cap, no new request is allowed. Manual Git
    # checks remain possible and must not overwrite the GitHub cooldown.
    for elapsed in (3600, 90000, 172799):
        github_clock.elapsed = elapsed
        opener.open.reset_mock()
        with env.create(cfg, git_instance=env.git()) as engine:
            calls = len(engine.git.calls)
            engine.run()
            opener.open.assert_not_called()
            assert engine.sources == cfg.sources
            assert len(engine.git.calls) > calls
            assert engine.store.data["discovery"]["retry_at"] == until
            assert engine.repos[env.key] == before
    github_clock.elapsed = 172800
    opener.open.side_effect = [api_response({"login": "fictional"}), api_response([])]
    # No real waiting when checking the next independently scheduled discovery.
    with env.create(
        replace(cfg, discovery=Discovery(0)), git_instance=env.git()
    ) as engine:
        engine.run()
        assert opener.open.call_count == 2
        assert engine.store.data["discovery"]["state"] == "completed"
        assert engine.store.data["discovery"]["retry_at"] == 0


def test_engine_wires_shared_delay_shutdown_and_identity_verification(
    github_env, monkeypatch, github_clock, github_stop
):
    env = github_env
    cfg = replace(
        env.config,
        sources=(Source("manual", "https://github.com/fictional/project.git"),),
        discovery=Discovery(250),
    )
    monkeypatch.setattr("repoharbor.engine.GitHub", GitHub)
    opener = Mock()
    link = '<https://api.github.com/user/repos?per_page=100&page=2>; rel="next"'
    opener.open.side_effect = [
        api_response({"login": "fictional"}),
        api_response([raw_repo()], {"Link": link}),
        api_response([]),
        api_response({"login": "fictional"}),
        api_response([]),
        api_response(raw_repo()),
    ]
    monkeypatch.setattr("repoharbor.github.build_opener", lambda *args: opener)
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.git.stop = github_stop
        engine.discover()
        assert engine.store.data["discovery"]["state"] == "completed"
        assert github_stop.waits == [0.25] * 5
        assert (
            opener.open.call_args_list[-1]
            .args[0]
            .full_url.endswith("/repos/fictional/project")
        )


def test_engine_discards_partial_discovery_when_delay_exhausts_deadline(
    github_env, monkeypatch, github_clock, github_stop
):
    env = github_env
    cfg = replace(env.config, discovery=Discovery(60000))
    monkeypatch.setattr("repoharbor.engine.GitHub", GitHub)
    opener = Mock()
    opener.open.side_effect = [
        api_response({"login": "fictional"}),
        api_response([raw_repo()]),
    ]
    monkeypatch.setattr("repoharbor.github.build_opener", lambda *args: opener)
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.git.stop = github_stop
        engine.discover()
        assert opener.open.call_count == 2
        assert github_clock.elapsed == 120
        assert engine.store.data["discovery"]["state"] == "failed"
        assert engine.store.data["discovery"]["code"] == "GITHUB_DISCOVERY_TIMEOUT"
        assert engine.sources == () and engine.repos == {}
