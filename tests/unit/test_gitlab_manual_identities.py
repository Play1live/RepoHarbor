from dataclasses import replace
from unittest.mock import Mock, call

import pytest

from repoharbor.config import Config, Provider
from repoharbor.github import DiscoveryError
from repoharbor.registry import gitlab_manual_identities

HOST = "gitlab.example.org"
NAME = HOST + "/group/beta"
KEY = "gitlab:" + HOST + ":18"


def config():
    return Config(
        (),
        ("first", "second", "third", "github", "other"),
        "env",
        providers=(
            Provider("github", "github"),
            Provider("one", "first", type="gitlab", host=HOST),
            Provider("other", "other", type="gitlab", host="other.example.org"),
            Provider("two", "second", type="gitlab", host=HOST),
            Provider("repeated", "first", type="gitlab", host=HOST),
            Provider("three", "third", type="gitlab", host=HOST),
        ),
    )


@pytest.mark.parametrize("misses", [0, 1, 2])
def test_only_404_advances_in_configuration_order_and_success_stops(misses):
    client = Mock()
    client.manual_identity.side_effect = [
        *(DiscoveryError("GITLAB_NOT_ACCESSIBLE") for _ in range(misses)),
        KEY,
    ]
    assert gitlab_manual_identities(client, config(), {NAME}, HOST) == {KEY}
    assert client.manual_identity.call_args_list == [
        call(NAME, credential)
        for credential in ("first", "second", "third")[: misses + 1]
    ]


def test_all_404_fails_closed_without_using_other_provider_credentials():
    client = Mock()
    client.manual_identity.side_effect = DiscoveryError("GITLAB_NOT_ACCESSIBLE")
    with pytest.raises(DiscoveryError, match="^GITLAB_MANUAL_IDENTITY_UNRESOLVED$"):
        gitlab_manual_identities(client, config(), {NAME}, HOST)
    assert client.manual_identity.call_args_list == [
        call(NAME, credential) for credential in ("first", "second", "third")
    ]


@pytest.mark.parametrize(
    "code",
    [
        "GITLAB_UNAUTHORIZED",
        "GITLAB_FORBIDDEN",
        "GITLAB_RATE_LIMIT",
        "GITLAB_INVALID_RESPONSE_OR_NETWORK",
        "GITLAB_HTTP_ERROR",
        "GITLAB_INVALID_PROJECT",
        "GITLAB_INVALID_ID",
        "GITLAB_INVALID_PAGINATION",
        "GITLAB_INCOMPLETE_PAGINATION",
        "INTERRUPTED",
    ],
)
@pytest.mark.parametrize("misses", [0, 1])
def test_other_api_errors_propagate_without_fallback(code, misses):
    client = Mock()
    error = DiscoveryError(code, retry_at=123456)
    client.manual_identity.side_effect = [
        *(DiscoveryError("GITLAB_NOT_ACCESSIBLE") for _ in range(misses)),
        error,
        KEY,
    ]
    with pytest.raises(DiscoveryError) as caught:
        gitlab_manual_identities(client, config(), {NAME}, HOST)
    assert caught.value is error
    assert caught.value.retry_at == 123456
    assert client.manual_identity.call_args_list == [
        call(NAME, credential) for credential in ("first", "second")[: misses + 1]
    ]


def test_multiple_addresses_sorted_and_stable_ids_deduplicated():
    client = Mock()
    alias = HOST + "/group/alias"
    # Distinct addresses can resolve to the same transferred project identity.
    client.manual_identity.side_effect = [
        KEY,
        DiscoveryError("GITLAB_NOT_ACCESSIBLE"),
        KEY,
    ]
    assert gitlab_manual_identities(client, config(), {NAME, alias, NAME}, HOST) == {
        KEY
    }
    assert client.manual_identity.call_args_list == [
        call(alias, "first"),
        call(NAME, "first"),
        call(NAME, "second"),
    ]


def test_matching_instance_only_and_no_credentials_fails_closed():
    client = Mock()
    client.manual_identity.return_value = "gitlab:other.example.org:18"
    name = "other.example.org/group/beta"
    assert gitlab_manual_identities(client, config(), {name}, "other.example.org") == {
        "gitlab:other.example.org:18"
    }
    client.manual_identity.assert_called_once_with(name, "other")
    client.reset_mock()
    with pytest.raises(DiscoveryError, match="MANUAL_IDENTITY_UNRESOLVED"):
        gitlab_manual_identities(client, replace(config(), providers=()), {NAME}, HOST)
    client.manual_identity.assert_not_called()
    assert gitlab_manual_identities(client, config(), set(), HOST) == set()
