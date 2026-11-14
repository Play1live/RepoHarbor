import copy
import io
import time
from dataclasses import replace
from email.message import Message
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
import yaml
from conftest import raw_repo

from repoharbor import __version__
from repoharbor.config import Config, Provider, Source, load
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError
from repoharbor.github import (
    MAX_BODY,
    DiscoveryError,
    GitHub,
    NoRedirect,
    parse_repository,
    retry_time,
)
from repoharbor.persistence import metadata, validate_state
from repoharbor.providers import Observation
from repoharbor.registry import (
    manual_addresses,
    manual_identities,
    plan,
    stored_repository,
)
from repoharbor.selection import Selection, address, repository_key, selector


def config(**kwargs):
    return Config(
        (),
        ("first", "second"),
        "env",
        providers=(Provider("one", "first"), Provider("two", "second")),
        selection=Selection(mode="all"),
        **kwargs,
    )


def observation(raw=None, credential="first", scope=True):
    return Observation(parse_repository(raw or raw_repo()), credential, scope)


def test_config_additive_defaults_and_stable_override():
    def settings(data):
        return load(
            {
                "REPOHARBOR_TOKEN_SOURCE": "env",
                "REPOHARBOR_CONFIG": yaml.safe_dump({"schema_version": 1, **data}),
            }
        )

    assert settings({"providers": []}).selection == Selection()
    cfg = settings(
        {
            "credentials": ["first"],
            "providers": [
                {
                    "id": "example",
                    "type": "github",
                    "credential": "first",
                    "include_owned": False,
                }
            ],
            "selection": {
                "mode": "selected",
                "selected": [
                    "https://github.com/Fictional/Project.git",
                    "github:github.com:17",
                ],
                "visibility": ["private"],
            },
            "storage": {"overrides": {"github:github.com:17": 4}},
        }
    )
    assert cfg.providers == (Provider("example", "first", False),)
    assert cfg.selection.selected == (
        "github.com/fictional/project",
        "github:github.com:17",
    )
    assert cfg.storage.overrides["github:github.com:17"] == 4 * 1024**3


@pytest.mark.parametrize(
    "change",
    [
        {"providers": {}},
        {"providers": [{"id": "one", "type": "unsupported", "credential": "first"}]},
        {
            "providers": [
                {
                    "id": "one",
                    "type": "github",
                    "host": "evil.example.org",
                    "credential": "first",
                }
            ]
        },
        {"providers": [{"id": "one", "type": "github", "credential": "missing"}]},
        {
            "providers": [
                {
                    "id": "one",
                    "type": "github",
                    "credential": "first",
                    "include_owned": 1,
                }
            ]
        },
        {"providers": [{"id": "one", "type": "github", "credential": "first"}] * 2},
        {"selection": {"mode": "unknown"}},
        {"selection": {"visibility": []}},
        {"selection": {"visibility": "public"}},
        {"selection": {"visibility": ["unsupported"]}},
        {"selection": {"include_forks": 1}},
        {"selection": {"include_archived": None}},
        {"selection": {"selected": "17"}},
        {"selection": {"excluded": [17]}},
        {"selection": {"unknown": True}},
        {"storage": {"overrides": {"github:github.com:0": 1}}},
    ],
)
def test_invalid_discovery_configuration(change):
    with pytest.raises(HarborError):
        load(
            {
                "REPOHARBOR_TOKEN_SOURCE": "env",
                "REPOHARBOR_CONFIG": yaml.safe_dump(
                    {"schema_version": 1, "credentials": ["first"], **change}
                ),
            }
        )


@pytest.mark.parametrize(
    "value",
    [
        None,
        17,
        "github:evil:17",
        "github:github.com:01",
        "github:github.com:0",
        "github:github.com:../17",
    ],
)
def test_invalid_identity(value):
    with pytest.raises(HarborError):
        repository_key(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "github.com/a/..",
        "github.com/a/b/extra",
        "https://token@github.com/a/b",
        "http://github.com/a/b",
        "github.com/a/b?token=fake",
        "github.com/a/%2e",
    ],
)
def test_invalid_address(value):
    with pytest.raises(HarborError):
        address(value)


def test_selection_precedence_and_historical_aliases():
    key = repository_key("github:github.com:17")
    names = {"github.com/fictional/project", "github.com/fictional/old"}
    assert selector(key) == key
    assert selector("https://github.com/Fictional/Project.git/") in names
    for mode in ("all", "all_except", "selected"):
        selected = Selection(mode=mode, selected=(key,))
        assert selected.reason(key, names, "public", False, False) is None
        assert (
            replace(selected, excluded=(key,)).reason(
                key, names, "public", False, False
            )
            == "explicit_exclusion"
        )
        assert (
            replace(selected, visibility=("public",)).reason(
                key, names, "private", False, False
            )
            == "visibility_filter"
        )
        assert selected.reason(key, names, "public", True, False) == "fork_filter"
        assert selected.reason(key, names, "public", False, True) == "archived_filter"
        assert (
            replace(selected, include_forks=True, include_archived=True).reason(
                key, names, "public", True, True
            )
            is None
        )
    assert Selection().reason(key, names, "public", False, False) == "not_selected"
    assert (
        Selection(selected=("github.com/fictional/old",)).reason(
            key, names, "public", False, False
        )
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        {"id": True},
        {"id": -1},
        {"id": "17"},
        {"id": 10**21},
        {"owner": {}},
        {"owner": {"login": "fictional", "type": "Bot"}},
        {"name": "../evil"},
        {"full_name": "other/project"},
        {"private": 1},
        {"fork": None},
        {"archived": "false"},
        {"visibility": "private"},
        {"clone_url": "https://evil.example.org/a.git"},
        {"html_url": "https://github.com/other/project"},
        {"default_branch": 17},
        {"default_branch": "x" * 256},
        {"created_at": "bad"},
    ],
)
def test_malformed_repository_fails_closed(change):
    with pytest.raises(DiscoveryError, match="INVALID_REPOSITORY"):
        parse_repository(raw_repo(**change))


def test_empty_default_branch_and_nonobject():
    assert parse_repository(raw_repo(default_branch=None)).default_branch is None
    for value in (None, [], {}, "fake-token"):
        with pytest.raises(DiscoveryError) as error:
            parse_repository(value)
        assert "fake-token" not in str(error.value)


def test_transport_secret_scope_size_json_network_and_deadline():
    client = GitHub(Credentials({"first": "fictional-secret"}), request_delay_ms=0)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.headers = {"Link": ""}
    response.read.side_effect = [b'{"login":"fictional"}', b""]
    client.opener.open = Mock(return_value=response)
    assert client.request("/user", "first")[0] == {"login": "fictional"}
    request = client.opener.open.call_args.args[0]
    assert request.full_url == "https://api.github.com/user"
    assert request.get_header("User-agent") == f"RepoHarbor/{__version__}"
    assert request.get_header("Authorization") == "Bearer fictional-secret"
    assert "fictional-secret" not in request.full_url
    assert client.opener.open.call_args.kwargs["timeout"] == 15
    assert (
        NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://evil.example.org"
        )
        is None
    )
    for parts, code in (
        ([b"invalid", b""], "INVALID_RESPONSE"),
        ([b"x" * (MAX_BODY + 1)], "TOO_LARGE"),
    ):
        response.read.side_effect = parts
        with pytest.raises(DiscoveryError, match=code):
            client.request("/user", "first")
    client.opener.open.side_effect = URLError("fictional-secret")
    with pytest.raises(DiscoveryError) as error:
        client.request("/user", "first")
    assert "fictional-secret" not in str(error.value)
    client.deadline = 0
    with pytest.raises(DiscoveryError, match="TIMEOUT"):
        client.request("/user", "first")


@pytest.mark.parametrize(
    "status,headers,body,expected",
    [
        (401, {}, b"fictional-secret", "UNAUTHORIZED"),
        (403, {}, b"forbidden", "FORBIDDEN"),
        (404, {}, b"missing", "NOT_ACCESSIBLE"),
        (500, {}, b"error", "HTTP_ERROR"),
        (301, {}, b"moved", "HTTP_ERROR"),
        (429, {"Retry-After": "120"}, b"", "RATE_LIMIT"),
        (
            403,
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "99999999999"},
            b"",
            "RATE_LIMIT",
        ),
        (403, {}, b"secondary rate limit", "RATE_LIMIT"),
    ],
)
def test_http_error_redaction_and_rate_limits(status, headers, body, expected):
    client = GitHub(Credentials({"first": "fictional-secret"}))
    message = Message()
    for key, value in headers.items():
        message[key] = value
    client.opener.open = Mock(
        side_effect=HTTPError(
            "https://api.github.com/user",
            status,
            "fictional-secret",
            message,
            io.BytesIO(body),
        )
    )
    with pytest.raises(DiscoveryError, match=expected) as error:
        client.request("/user", "first")
    assert "fictional-secret" not in str(error.value)
    if expected == "RATE_LIMIT":
        expected_retry = (
            99999999999
            if "X-RateLimit-Reset" in headers
            else time.time() + (120 if status == 429 else 60)
        )
        assert error.value.retry_at == pytest.approx(expected_retry, abs=1)
    else:
        assert error.value.retry_at == 0


def test_retry_after_dates_and_malformed_values():
    assert retry_time({"Retry-After": "Thu, 01 Jan 1970 00:03:00 GMT"}, 0) == 180
    assert retry_time({"Retry-After": "invalid", "X-RateLimit-Reset": "bad"}, 0) == 60
    assert retry_time({"Retry-After": "-1"}, 0) == 60


def test_discovery_pagination_and_scope_union():
    client = GitHub(Credentials({}))
    user = {"login": "fictional"}
    org = raw_repo(18, "fictional-org")
    org["owner"]["type"] = "Organization"
    collaborator = raw_repo(19, "someone")
    link = '<https://api.github.com/user/repos?per_page=100&page=2>; rel="next"'
    client.request = Mock(
        side_effect=[
            (user, {}),
            ([raw_repo(), org], {"Link": link}),
            ([collaborator], {}),
        ]
    )
    result = client.discover(Provider("one", "first", False, True, False))
    assert [x.in_scope for x in result] == [False, True, False]
    assert all(x.credential == "first" for x in result)
    assert client.request.call_args_list[-1].args[0].endswith("page=2")
    client.request = Mock(
        side_effect=[(user, {}), ([raw_repo(i + 1) for i in range(100)], {}), ([], {})]
    )
    assert len(client.discover(Provider("one", "first"))) == 100


@pytest.mark.parametrize(
    "link",
    [
        '<http://api.github.com/user/repos?per_page=100&page=2>; rel="next"',
        '<https://evil.example.org/user/repos?per_page=100&page=2>; rel="next"',
        '<https://api.github.com/other?per_page=100&page=2>; rel="next"',
        '<https://api.github.com/user/repos?per_page=100&page=1>; rel="next"',
        '<https://api.github.com/user/repos?per_page=100&page=2&x=1>; rel="next"',
        '<https://api.github.com/user/repos?per_page=100&page=2#fragment>; rel="next"',
        (
            '<https://api.github.com/user/repos?per_page=100&page=2>; rel="next", '
            '<https://evil.example.org>; rel="next"'
        ),
    ],
)
def test_pagination_cannot_forward_token_or_loop(link):
    with pytest.raises(DiscoveryError, match="PAGINATION"):
        GitHub.next_page(link, 1)


@pytest.mark.parametrize(
    "user,page,code",
    [
        ([], [], "INVALID_USER"),
        ({}, [], "INVALID_USER"),
        ({"login": "fictional"}, {}, "INVALID_PAGE"),
        ({"login": "fictional"}, [{}] * 101, "INVALID_PAGE"),
        (
            {"login": "fictional"},
            [raw_repo(), raw_repo(name="renamed")],
            "INCONSISTENT_ID",
        ),
    ],
)
def test_incomplete_discovery_never_returns_partial_success(user, page, code):
    client = GitHub(Credentials({}))
    client.request = Mock(side_effect=[(user, {}), (page, {})])
    with pytest.raises(DiscoveryError, match=code):
        client.discover(Provider("one", "first"))


def test_duplicate_id_and_pagination_limit():
    client = GitHub(Credentials({}))
    client.request = Mock(
        side_effect=[({"login": "fictional"}, {}), ([raw_repo(), raw_repo()], {})]
    )
    assert len(client.discover(Provider("one", "first"))) == 1
    client.request = Mock(
        side_effect=lambda path, credential: (
            ({"login": "fictional"}, {})
            if path == "/user"
            else ([raw_repo()] * 100, {})
        )
    )
    with pytest.raises(DiscoveryError, match="PAGINATION_LIMIT"):
        client.discover(Provider("one", "first"))


def test_registry_dedup_rename_filters_and_credential_order():
    cfg = config()
    original, sources = plan(
        cfg, {}, [observation(scope=False), observation(credential="second")], set()
    )
    key = sources[0].alias
    assert len(sources) == 1 and sources[0].credential == "second"
    before = copy.deepcopy(original)
    original[key]["backup"]["successful_updates"] = 3
    renamed = raw_repo(owner="fictional-org", name="renamed")
    proposed, sources = plan(
        cfg, original, [observation(renamed), observation(renamed, "second")], set()
    )
    assert len(sources) == 1 and sources[0].credential == "first"
    assert sources[0].alias == key
    assert proposed[key]["backup"]["successful_updates"] == 3
    assert proposed[key]["repository"]["aliases"] == [
        "github.com/fictional-org/renamed",
        "github.com/fictional/project",
    ]
    assert original[key]["repository"] == before[key]["repository"]
    assert stored_repository(proposed[key], key).name == "renamed"
    excluded = replace(
        cfg, selection=Selection(mode="all", excluded=("github.com/fictional/project",))
    )
    metas, sources = plan(excluded, proposed, [observation(renamed)], set())
    assert sources == () and metas[key]["selection"]["reason"] == "explicit_exclusion"
    unavailable, sources = plan(cfg, proposed, [], set())
    assert sources == ()
    assert unavailable[key]["source"]["discovery_status"] == "unavailable"
    assert unavailable[key]["source"]["status"] == "unknown"
    assert unavailable[key]["selection"]["status"] == "included"
    disabled, _ = plan(replace(cfg, providers=()), proposed, [], set())
    assert disabled[key]["selection"]["reason"] == "provider_disabled"


@pytest.mark.parametrize("status", ["unknown", "unavailable", "available"])
def test_discovery_preserves_git_status_errors_and_source_extensions(
    monkeypatch, status
):
    known, _ = plan(config(), {}, [observation()], set())
    key = next(iter(known))
    previous = {
        "status": status,
        "last_seen_at": "2026-01-01T00:00:00Z",
        "last_git_attempt_at": "2026-01-02T00:00:00Z",
        "diagnostic": {"code": "GIT_FAILED", "at": "2026-01-02T00:00:00Z"},
    }
    known[key]["source"] = copy.deepcopy(previous)
    known[key]["errors"]["total"] = 1
    original = copy.deepcopy(known)
    monkeypatch.setattr("repoharbor.registry.utcnow", lambda: "2026-01-03T00:00:00Z")
    proposed, _ = plan(config(), known, [observation()], set())
    assert proposed[key]["source"] == {
        **previous,
        "last_seen_at": "2026-01-03T00:00:00Z",
        "discovery_status": "available",
    }
    assert proposed[key]["errors"] == original[key]["errors"]
    assert known == original
    absent, sources = plan(config(), proposed, [], set())
    assert sources == ()
    assert absent[key]["source"] == {
        **proposed[key]["source"],
        "discovery_status": "unavailable",
    }


def test_new_repository_api_success_does_not_confirm_git():
    proposed, sources = plan(config(), {}, [observation()], set())
    assert len(sources) == 1
    assert proposed[sources[0].alias]["source"]["status"] == "unknown"
    assert proposed[sources[0].alias]["source"]["last_seen_at"]
    assert proposed[sources[0].alias]["source"]["discovery_status"] == "available"


def test_registry_conflicts_and_scope():
    cfg = config()
    known, _ = plan(cfg, {}, [observation()], set())
    key = next(iter(known))
    with pytest.raises(DiscoveryError, match="INCONSISTENT_ID"):
        plan(cfg, known, [observation(), observation(raw_repo(name="other"))], set())
    with pytest.raises(DiscoveryError, match="ALIAS_CONFLICT"):
        plan(cfg, known, [observation(raw_repo(rid=18))], set())
    conflict = copy.deepcopy(known)
    conflict["github:github.com:18"] = copy.deepcopy(conflict[key])
    with pytest.raises(DiscoveryError, match="ALIAS_CONFLICT"):
        plan(cfg, conflict, [], set())
    scoped, sources = plan(cfg, known, [observation(scope=False)], set())
    assert not sources and scoped[key]["selection"]["reason"] == "scope_filter"
    manual = {
        "manual": metadata(
            "manual", "https://github.com/fictional/project.git", None, None
        )
    }
    for catalog, ids in ((manual, set()), ({}, {key})):
        metas, sources = plan(cfg, catalog, [observation()], ids)
        assert (
            not sources
            and metas[key]["selection"]["reason"] == "manual_identity_conflict"
        )


def test_manual_resolution_only_blocks_no_adoption():
    cfg = replace(
        config(), sources=(Source("manual", "https://github.com/fictional/old.git"),)
    )
    client = Mock()
    client.request.side_effect = [
        DiscoveryError("GITHUB_NOT_ACCESSIBLE"),
        (raw_repo(), {}),
    ]
    names = manual_addresses(cfg, {})
    assert names == {"github.com/fictional/old"}
    assert manual_identities(client, cfg, names) == {"github:github.com:17"}
    client.request.side_effect = DiscoveryError("GITHUB_NOT_ACCESSIBLE")
    with pytest.raises(DiscoveryError, match="UNRESOLVED"):
        manual_identities(client, cfg, names)
    client.request.side_effect = DiscoveryError("GITHUB_RATE_LIMIT")
    with pytest.raises(DiscoveryError, match="RATE_LIMIT"):
        manual_identities(client, cfg, names)
    with pytest.raises(DiscoveryError, match="UNRESOLVED"):
        manual_addresses(
            replace(
                cfg, sources=(Source("bad", "https://github.com/fictional/too/many"),)
            ),
            {},
        )


@pytest.mark.parametrize(
    "change",
    [
        {"host": "evil.example.org"},
        {"id": "18"},
        {"provider": "manual"},
        {"aliases": []},
        {"aliases": "bad"},
        {"aliases": ["github.com/fictional/other"]},
    ],
)
def test_stored_identity_validation(change):
    known, _ = plan(config(), {}, [observation()], set())
    key = next(iter(known))
    known[key]["repository"].update(change)
    with pytest.raises(DiscoveryError):
        stored_repository(known[key], key)


@pytest.mark.parametrize(
    "change",
    [
        {"state": "bad"},
        {"retry_at": "secret"},
        {"retry_at": float("inf")},
        {"retry_at": -1},
        {"retry_at": True},
        {"started_at": 17},
    ],
)
def test_discovery_state_must_validate_before_startup_write(tmp_path, change):
    from repoharbor.persistence import Store

    with Store(tmp_path) as store:
        state = copy.deepcopy(store.data)
    state["discovery"] = {
        "state": "failed",
        "started_at": "2026-01-01T00:00:00Z",
        **change,
    }
    with pytest.raises(HarborError):
        validate_state(state)
    state["discovery"] = {"state": "running", "started_at": "2026-01-01T00:00:00Z"}
    validate_state(state)
