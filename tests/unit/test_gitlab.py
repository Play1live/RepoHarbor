import copy
import io
import time
from dataclasses import replace
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import urlencode

import pytest
import yaml
from conftest import api_response, raw_project

from repoharbor.config import Provider, load
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError
from repoharbor.github import MAX_BODY, DiscoveryError
from repoharbor.gitlab import (
    GitLab,
    cooldown,
    numeric_id,
    parse_project,
    quota_exhausted,
)
from repoharbor.persistence import validate_state
from repoharbor.providers import Observation
from repoharbor.registry import plan, stored_repository
from repoharbor.selection import Selection, address, instance_host, repository_key

HOST = "gitlab.example.org"


def client():
    c = GitLab(Credentials({"first": "fictional-secret"}), request_delay_ms=0)
    c.bind(HOST)
    return c


def settings(**extra):
    return load(
        {
            "REPOHARBOR_TOKEN_SOURCE": "env",
            "REPOHARBOR_CONFIG": yaml.safe_dump(
                {
                    "schema_version": 1,
                    "credentials": ["first", "second"],
                    **extra,
                }
            ),
        }
    )


def provider(**extra):
    return {"id": "one", "type": "gitlab", "host": HOST, "credential": "first", **extra}


def test_mixed_config_defaults_and_flags():
    cfg = settings(
        providers=[
            provider(include_owned=False, include_groups=False, include_shared=False),
            provider(id="two", credential="second"),
        ]
    )
    assert len(cfg.providers) == 2
    assert not cfg.providers[0].include_groups
    assert not cfg.providers[0].include_shared
    assert cfg.selection == Selection()
    assert (
        settings(providers=[provider(host="gitlab.com")]).providers[0].host
        == "gitlab.com"
    )
    cfg = settings(
        providers=[
            provider(),
            {
                "id": "github",
                "type": "github",
                "credential": "second",
            },
        ],
        selection={"visibility": ["internal"]},
    )
    assert cfg.selection.visibility == ("internal",)
    assert cfg.selection.reason("github:github.com:1", set(), "public", False, False)
    cfg = settings(
        providers=[provider(host=HOST + ":8443")],
        storage={"overrides": {f"gitlab:{HOST}:8443:17": 0}},
    )
    assert cfg.providers[0].host.endswith(":8443")
    assert repository_key(f"gitlab:{HOST}:8443:17").endswith(":17")
    assert (
        address(f"https://{HOST}:8443/group/sub/project.git")
        == f"{HOST}:8443/group/sub/project"
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"host": "http://gitlab.example.org"},
        {"host": "gitlab.example.org/"},
        {"host": "GitLab.example.org"},
        {"host": HOST + ":0"},
        {"host": HOST + ":65536"},
        {"host": HOST + ":443"},
        {"host": "token@" + HOST},
        {"host": "gitlab..example.org"},
        {"host": "-bad.example.org"},
        {"host": "localhost"},
        {"host": "github.com"},
        {"host": "api.github.com"},
        {"include_shared": 1},
        {"include_groups": None},
        {"include_organizations": True},
        {"include_collaborators": False},
        {"unknown": False},
        {"credential": "missing"},
        {"id": "../bad"},
    ],
)
def test_invalid_configuration(extra):
    with pytest.raises(HarborError):
        settings(providers=[provider(**extra)])


def test_credential_cannot_cross_services_or_manual_hosts():
    with pytest.raises(HarborError, match="another provider host"):
        settings(providers=[provider(), provider(id="other", host="other.example.org")])
    with pytest.raises(HarborError, match="another host"):
        settings(
            providers=[provider()],
            manual_git_urls=[
                {
                    "alias": "manual",
                    "url": "https://other.example.org/a.git",
                    "credential": "first",
                }
            ],
        )
    assert settings(providers=[provider(), provider(id="other")]).providers


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        "",
        "gitlab:x:1",
        f"gitlab:{HOST}:0",
        f"gitlab:{HOST}:01",
        f"gitlab:{HOST}:../1",
    ],
)
def test_invalid_identity(value):
    with pytest.raises(HarborError):
        repository_key(value)


@pytest.mark.parametrize(
    "value", [None, "", 123, "bad", "x." + "x" * 64, "a.-b", "a.b:0001", "a.b:443"]
)
def test_invalid_host(value):
    with pytest.raises(HarborError):
        instance_host(value)


@pytest.mark.parametrize(
    "extra",
    [
        {"id": True},
        {"id": 0},
        {"id": 10**20},
        {"id": "1"},
        {"namespace": {}},
        {"namespace": {"id": 1, "kind": "bot", "full_path": "abc"}},
        {"namespace": {"id": 1, "kind": "user", "full_path": "group/sub"}},
        {"namespace": {"id": 1, "kind": "group", "full_path": None}},
        {"path": "../bad"},
        {"path": "nested/repo"},
        {"path_with_namespace": "other/repo"},
        {"http_url_to_repo": "https://foreign.example.org/a.git"},
        {"http_url_to_repo": f"http://{HOST}/fictional/project.git"},
        {"web_url": f"https://{HOST}/wrong/project"},
        {"visibility": "secret"},
        {"archived": 1},
        {"forked_from_project": {"id": False}},
        {"default_branch": 1},
        {"default_branch": "x" * 256},
        {"default_branch": "a\nb"},
        {"created_at": "yesterday"},
    ],
)
def test_malformed_projects(extra):
    with pytest.raises(DiscoveryError, match="INVALID_PROJECT"):
        parse_project(raw_project(**extra), HOST)


def test_project_roundtrip_nested_namespace_and_filters(gitlab_env):
    cfg = replace(
        gitlab_env.config,
        selection=Selection(
            mode="selected",
            selected=(gitlab_env.key,),
            visibility=("internal",),
        ),
    )
    repo = parse_project(
        raw_project(
            owner="group/sub/deeper",
            kind="group",
            visibility="internal",
            default_branch=None,
        ),
        HOST,
    )
    obs = Observation(repo, "first", True)
    proposed, sources = plan(
        cfg, {}, [obs, obs], set(), provider_type="gitlab", host=HOST
    )
    assert len(sources) == 1
    assert stored_repository(proposed[repo.key], repo.key) == repo
    proposed[repo.key]["backup"]["successful_updates"] = 3
    renamed = parse_project(
        raw_project(
            owner="transferred/sub", kind="group", name="renamed", visibility="internal"
        ),
        HOST,
    )
    after, sources = plan(
        cfg,
        proposed,
        [Observation(renamed, "first", True)],
        set(),
        provider_type="gitlab",
        host=HOST,
    )
    assert len(after[repo.key]["repository"]["aliases"]) == 2
    assert after[repo.key]["backup"]["successful_updates"] == 3
    assert sources[0].alias == repo.key
    for scope, forks, archived, excluded, reason in [
        (False, False, False, (), "scope_filter"),
        (True, True, False, (), "fork_filter"),
        (True, False, True, (), "archived_filter"),
        (True, False, False, (repo.key,), "explicit_exclusion"),
    ]:
        item = replace(repo, fork=forks, archived=archived)
        metas, selected = plan(
            replace(cfg, selection=replace(cfg.selection, excluded=excluded)),
            {},
            [Observation(item, "first", scope)],
            set(),
            provider_type="gitlab",
            host=HOST,
        )
        assert not selected
        assert metas[repo.key]["selection"]["reason"] == reason


def test_transport_auth_cooldown_limits_and_redaction():
    c = client()
    c.opener.open = Mock(
        return_value=api_response(
            {"id": 1},
            {
                "rAtElImIt-ReMaInInG": "0",
                "RateLimit-Reset": str(time.time() + 3600),
            },
        )
    )
    assert c.request("/user", "first")[0]["id"] == 1
    request = c.opener.open.call_args.args[0]
    assert request.full_url == f"https://{HOST}/api/v4/user"
    assert request.get_header("Private-token") == "fictional-secret"
    assert request.get_header("Authorization") is None
    with pytest.raises(DiscoveryError, match="GITLAB_RATE_LIMIT"):
        c.request("/user", "first")
    assert c.opener.open.call_count == 1
    for parts, code in [
        ([b"x" * (MAX_BODY + 1)], "TOO_LARGE"),
        ([b"not json", b""], "INVALID_RESPONSE"),
        (IncompleteRead(b"fictional-secret"), "INVALID_RESPONSE"),
    ]:
        c = client()
        response = Mock(headers={}, **{"__enter__": Mock(), "__exit__": Mock()})
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.side_effect = parts
        c.opener.open = Mock(return_value=response)
        with pytest.raises(DiscoveryError, match=code) as error:
            c.request("/user", "first")
        assert "fictional-secret" not in str(error.value)
    for path in ("//foreign.example.org", "https://foreign.example.org", "/user#x"):
        with pytest.raises(DiscoveryError, match="ENDPOINT"):
            client().request(path, "first")


@pytest.mark.parametrize(
    "status,expected",
    [
        (301, "HTTP_ERROR"),
        (302, "HTTP_ERROR"),
        (401, "UNAUTHORIZED"),
        (403, "FORBIDDEN"),
        (404, "NOT_ACCESSIBLE"),
        (429, "RATE_LIMIT"),
        (500, "HTTP_ERROR"),
        (503, "HTTP_ERROR"),
    ],
)
def test_http_failures(status, expected):
    c = client()
    c.opener.open = Mock(
        side_effect=HTTPError(
            "",
            status,
            "fictional-secret",
            {},
            io.BytesIO(b"fictional-secret"),
        )
    )
    with pytest.raises(DiscoveryError, match="GITLAB_" + expected):
        c.request("/user", "first")
    assert c.opener.open.call_count == 1


def test_rate_headers_dates_duplicates_and_fallbacks():
    assert cooldown({"RateLimit-Reset": "300", "Retry-After": "200"}, 100) == 300
    assert (
        cooldown({"RateLimit-ResetTime": "Thu, 01 Jan 1970 00:06:00 GMT"}, 100) == 360
    )
    assert cooldown({"RateLimit-ResetTime": "bad"}, 100, require_reset=True) == 160
    assert cooldown({"RateLimit-Reset": "NaN", "Retry-After": "-1"}, 100) == 160
    headers = Message()
    headers["RateLimit-Remaining"] = "9"
    headers["ratelimit-remaining"] = "00"
    assert quota_exhausted(headers)
    assert not quota_exhausted({"RateLimit-Remaining": "bad"})
    assert cooldown({"RateLimit-Reset": "500", "Retry-After": "800"}, 100) == 900
    for value in (None, True, 0, -1, "1"):
        with pytest.raises(DiscoveryError):
            numeric_id(value)


def project_link(cursor="17", **extra):
    params = {
        "per_page": "100",
        "order_by": "id",
        "sort": "asc",
        "membership": "true",
        "pagination": "keyset",
        "id_after": cursor,
        **extra,
    }
    return f'<https://{HOST}/api/v4/projects?{urlencode(params)}>; rel="next"'


def test_keyset_and_offset_pagination():
    c = client()
    c.request = Mock(
        side_effect=[
            ([raw_project()], {"LiNk": project_link()}),
            ([], {}),
        ]
    )
    assert len(c.listing("/projects", {"membership": "true"}, "first")) == 1
    assert "id_after=17" in c.request.call_args.args[0]
    c = client()
    c.request = Mock(
        side_effect=[
            ([{"id": 1}], {"X-Next-Page": "2", "X-Page": "1", "X-Total-Pages": "2"}),
            ([], {"X-Page": "2", "X-Next-Page": ""}),
        ]
    )
    assert c.listing("/groups", {"all_available": "false"}, "first") == [{"id": 1}]


@pytest.mark.parametrize(
    "link",
    [
        project_link().replace(HOST, "foreign.example.org"),
        project_link().replace("https:", "http:"),
        project_link().replace("/projects?", "/user?"),
        project_link("0"),
        project_link(extra="bad"),
        project_link(cursor="-1"),
        "garbage",
        project_link() + ", " + project_link(),
        project_link().replace(">; rel=", "#fragment>; rel="),
    ],
)
def test_malicious_pagination(link):
    c = client()
    c.request = Mock(return_value=([raw_project()], {"Link": link}))
    with pytest.raises(DiscoveryError, match="PAGINATION"):
        c.listing("/projects", {"membership": "true"}, "first")
    assert c.request.call_count == 1


@pytest.mark.parametrize(
    "values,headers",
    [
        ({}, {}),
        ([{}] * 101, {}),
        ([{}] * 100, {}),
        ([], {"X-Next-Page": "2"}),
        ([], {"X-Page": "9"}),
        ([], {"X-Total-Pages": "bad"}),
        ([], {"X-Total-Pages": "2"}),
        ([], {"X-Next-Page": "3"}),
    ],
)
def test_incomplete_pagination(values, headers):
    c = client()
    c.request = Mock(return_value=(values, headers))
    with pytest.raises(DiscoveryError):
        c.listing("/groups", {}, "first")


def test_pagination_budget_and_invalid_user():
    c = client()
    c.pages = 1000
    with pytest.raises(DiscoveryError, match="PAGINATION_LIMIT"):
        c.listing("/groups", {}, "first")
    for user in (
        None,
        {},
        {"id": 1, "username": None},
        {"id": 1, "username": "bot", "bot": True},
    ):
        c = client()
        c.request = Mock(return_value=(user, {}))
        with pytest.raises(DiscoveryError):
            c.discover(Provider("one", "first"))


def test_owned_group_subgroups_shared_and_scope_evidence():
    personal = raw_project()
    group = raw_project(18, "group/sub/deep", kind="group")
    direct = raw_project(19, "other-group", kind="group")
    shared = raw_project(20, "someone", kind="user")
    c = client()
    c.request = Mock(return_value=({"id": 1, "username": "fictional"}, {}))
    c.listing = Mock(
        side_effect=[
            [personal, direct],
            [personal],
            [{"id": 7}],
            [group],
            [group, shared],
        ]
    )
    p = Provider(
        "one",
        "first",
        include_owned=False,
        type="gitlab",
        host=HOST,
        include_groups=True,
        include_shared=False,
    )
    result = c.discover(p)
    assert {o.repository.key: o.in_scope for o in result} == {
        f"gitlab:{HOST}:17": False,
        f"gitlab:{HOST}:18": True,
        f"gitlab:{HOST}:19": False,
        f"gitlab:{HOST}:20": False,
    }
    assert c.listing.call_args_list[2].args[1] == {"all_available": "false"}
    assert c.listing.call_args_list[3].args[1]["include_subgroups"] == "true"
    c = client()
    c.request = Mock(return_value=(raw_project(owner="group/sub", kind="group"), {}))
    assert (
        c.manual_identity(f"{HOST}/group/sub/project", "first") == f"gitlab:{HOST}:17"
    )
    assert c.request.call_args.args[0] == "/projects/group%2Fsub%2Fproject"


def test_exact_full_pages_without_headers_are_proven_complete():
    for endpoint, params in (("/projects", {"membership": "true"}), ("/groups", {})):
        c = client()
        c.request = Mock(
            side_effect=[([{"id": n} for n in range(1, 101)], {}), ([], {})]
        )
        assert len(c.listing(endpoint, params, "first")) == 100
        assert c.request.call_count == 2
    c = client()
    c.request = Mock(return_value=([{"id": n} for n in range(1, 101)], {}))
    with pytest.raises(DiscoveryError, match="INCONSISTENT_PAGINATION"):
        c.listing("/projects", {"membership": "true"}, "first")


def test_inconsistent_group_project_and_provider_metadata_are_rejected(gitlab_env):
    c = client()
    c.request = Mock(return_value=({"id": 1, "username": "fictional"}, {}))
    c.listing = Mock(side_effect=[[raw_project()], [raw_project(name="changed")], []])
    with pytest.raises(DiscoveryError, match="INCONSISTENT_ID"):
        c.discover(Provider("one", "first"))
    c.listing = Mock(side_effect=[[], [], [{}]])
    with pytest.raises(DiscoveryError, match="INVALID_GROUP"):
        c.discover(Provider("one", "first"))
    cfg = gitlab_env.config
    with pytest.raises(DiscoveryError, match="IDENTITY_CONFLICT"):
        plan(
            cfg,
            {},
            [Observation(parse_project(raw_project(), HOST), "first", True)],
            set(),
        )


@pytest.mark.parametrize(
    "states",
    [
        [],
        {"bad": {}},
        {"gitlab:" + HOST: {}},
        {
            "gitlab:" + HOST: {
                "state": "running",
                "started_at": "now",
                "retry_at": float("nan"),
            }
        },
    ],
)
def test_corrupt_provider_cooldown_state_fails_closed(gitlab_env, states):
    env = gitlab_env
    with env.create(env.config, git_instance=env.git()) as engine:
        state = copy.deepcopy(engine.store.data)
        state["provider_discovery"] = states
        with pytest.raises(HarborError):
            validate_state(state)


def test_gitlab_delay_and_shutdown_reuse_shared_transport(github_clock, github_stop):
    c = GitLab(
        Credentials({"first": "fictional-secret"}),
        request_delay_ms=1000,
        stop=github_stop,
    )
    c.bind(HOST)
    c.opener.open = Mock(side_effect=[api_response({}), api_response({})])
    c.request("/user", "first")
    assert not github_stop.waits
    c.request("/user", "first")
    assert github_stop.waits == [1]
    github_stop.on_wait = lambda: setattr(github_stop, "stopped", True)
    with pytest.raises(DiscoveryError, match="INTERRUPTED"):
        c.request("/user", "first")
    assert c.opener.open.call_count == 2


def test_fork_marker_and_partial_success_status():
    assert parse_project(raw_project(mr_default_target_self=False), HOST).fork
    with pytest.raises(DiscoveryError):
        parse_project(raw_project(mr_default_target_self="false"), HOST)
    c = client()
    response = api_response({})
    response.status = 206
    c.opener.open = Mock(return_value=response)
    with pytest.raises(DiscoveryError, match="GITLAB_HTTP_ERROR"):
        c.request("/user", "first")


def test_gitlab_port_directory_roundtrip(gitlab_env):
    env = gitlab_env
    host = HOST + ":8443"
    cfg = replace(
        env.config, providers=tuple(replace(p, host=host) for p in env.config.providers)
    )
    env.results["first"] = env.results["second"] = [raw_project(host=host)]
    key = f"gitlab:{host}:17"
    with env.create(cfg, git_instance=env.git()) as engine:
        engine.discover()
        folder = engine.path(key)
        assert folder == cfg.root / "gitlab" / (HOST + "~8443") / "17"
        assert engine.path(env.key) != folder
        assert folder.joinpath("metadaten.json").is_file()
    with env.create(cfg, git_instance=env.git()) as engine:
        assert engine.repos[key]["repository"]["host"] == host


def test_unbound_client_cannot_send_private_token_to_inherited_github_base():
    c = GitLab(Credentials({"first": "fictional-secret"}), request_delay_ms=0)
    c.opener.open = Mock()
    with pytest.raises(DiscoveryError, match="GITLAB_UNBOUND_INSTANCE"):
        c.request("/user", "first")
    c.opener.open.assert_not_called()
