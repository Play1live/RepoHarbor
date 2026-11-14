import io
import math
import sys
import threading
from datetime import UTC, datetime
from email.message import Message
from email.utils import format_datetime
from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
import yaml
from conftest import api_response, raw_repo

from repoharbor.config import Provider, load
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError
from repoharbor.github import DiscoveryError, GitHub, retry_time


@pytest.mark.parametrize("name", ["retry-after", "ReTrY-AfTeR", "Retry-After"])
def test_rate_headers_preserve_long_server_cooldown(monkeypatch, name):
    monkeypatch.setattr("repoharbor.github.time.time", lambda: 1000)
    headers = Message()
    headers[name] = "172800"
    client = GitHub(Credentials({"first": "fictional-secret"}))
    client.opener.open = Mock(
        side_effect=HTTPError("", 429, "fictional-secret", headers, io.BytesIO())
    )
    with pytest.raises(DiscoveryError, match="RATE_LIMIT") as error:
        client.request("/user", "first")
    assert error.value.retry_at == 173800
    assert retry_time(dict(headers.items()), 1000) == 173800


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("failure", [IncompleteRead, BadStatusLine, RemoteDisconnected])
def test_protocol_read_errors_are_redacted_discovery_failures(status, failure):
    client = GitHub(Credentials({"first": "fictional-secret"}))
    body = Mock()
    body.closed = False
    body.read.side_effect = failure(b"fictional-secret response body")
    if status == 200:
        body.__enter__ = Mock(return_value=body)
        body.__exit__ = Mock(return_value=False)
        client.opener.open = Mock(return_value=body)
    else:
        client.opener.open = Mock(
            side_effect=HTTPError("", status, "fictional-secret", Message(), body)
        )
    with pytest.raises(DiscoveryError, match="INVALID_RESPONSE_OR_NETWORK") as error:
        client.request("/user", "first")
    assert "fictional-secret" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"retry-after": "120"}, 1120),
        ({"RETRY-AFTER": "5"}, 1005),
        ({"Retry-After": "0"}, 1000),
        ({"retry-after": "Thu, 01 Jan 1970 01:00:00 GMT"}, 3600),
        ({"x-RaTeLiMiT-ReSeT": "37000"}, 37000),
        ({"retry-after": "120", "X-RateLimit-Reset": "10000"}, 10000),
        ({"Retry-After": "172800", "x-ratelimit-reset": "10000"}, 173800),
        ({"retry-after": "invalid", "x-ratelimit-reset": "10000"}, 10000),
        ({"retry-after": "120", "x-ratelimit-reset": "invalid"}, 1120),
        ({}, 1060),
    ],
)
def test_retry_time_uses_latest_valid_header_case_insensitively(headers, expected):
    assert retry_time(headers, 1000) == expected


@pytest.mark.parametrize("name", ["retry-after", "x-ratelimit-reset"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "invalid",
        "-1",
        "nan",
        "NaN",
        "inf",
        "-Infinity",
        "1e9999",
        "9" * 10000,
        "Wed, 99 Jan 9999 00:00:00 GMT",
        "Thu, 01 Jan 1970 00:00:00 GMT",
        "Thu, 01 Jan 1970 01:00:00",
    ],
)
def test_unusable_wait_headers_have_safe_fallback(name, value):
    assert retry_time({name: value}, 1000) == 1060


def test_finite_extreme_cooldowns_and_repeated_headers():
    assert retry_time({"Retry-After": "1e300"}, 1000) == 1e300
    assert retry_time({"X-RateLimit-Reset": "1e300"}, 1000) == 1e300
    result = retry_time({"Retry-After": str(sys.float_info.max)}, sys.float_info.max)
    assert math.isfinite(result) and result == sys.float_info.max
    headers = Message()
    headers["Retry-After"] = "172800"
    headers["retry-after"] = "120"
    headers["x-ratelimit-reset"] = "10000"
    assert retry_time(headers, 1000) == 173800


@pytest.mark.parametrize(
    "headers,body,expected",
    [
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "37000"}, b"", 37000),
        ({"rEtRy-AfTeR": "120"}, b"", 1120),
        ({"Retry-After": "invalid"}, b"", 1060),
        ({}, b"secondary rate limit", 1060),
    ],
)
def test_403_rate_detection_and_fallback(monkeypatch, headers, body, expected):
    monkeypatch.setattr("repoharbor.github.time.time", lambda: 1000)
    client = GitHub(Credentials({"first": "fictional-secret"}))
    client.opener.open = Mock(
        side_effect=HTTPError("", 403, "", headers, io.BytesIO(body))
    )
    with pytest.raises(DiscoveryError, match="RATE_LIMIT") as error:
        client.request("/user", "first")
    assert error.value.retry_at == expected


@pytest.mark.parametrize("delay", [None, 0, 1, 250, 1000, 60000])
def test_request_delay_configuration_defaults_and_values(delay):
    data = {"schema_version": 1}
    if delay is not None:
        data["discovery"] = {"request_delay_ms": delay}
    cfg = load(
        {"REPOHARBOR_TOKEN_SOURCE": "env", "REPOHARBOR_CONFIG": yaml.safe_dump(data)}
    )
    assert cfg.discovery.request_delay_ms == (1000 if delay is None else delay)


@pytest.mark.parametrize(
    "block",
    [
        None,
        [],
        False,
        "1000",
        {"unknown": 1000},
        *(
            {"request_delay_ms": x}
            for x in [
                -1,
                60001,
                1.0,
                0.0,
                0.5,
                True,
                False,
                "1000",
                None,
                float("nan"),
                float("inf"),
            ]
        ),
    ],
)
def test_request_delay_configuration_rejects_invalid_values(block):
    with pytest.raises(HarborError):
        load(
            {
                "REPOHARBOR_TOKEN_SOURCE": "env",
                "REPOHARBOR_CONFIG": yaml.safe_dump(
                    {"schema_version": 1, "discovery": block}
                ),
            }
        )


def test_empty_discovery_block_retains_default():
    cfg = load(
        {
            "REPOHARBOR_TOKEN_SOURCE": "env",
            "REPOHARBOR_CONFIG": "schema_version: 1\ndiscovery: {}",
        }
    )
    assert cfg.discovery.request_delay_ms == 1000


@pytest.mark.parametrize("delay", [None, 0, 1, 250, 1000, 60000])
def test_delay_starts_after_completion_and_has_no_initial_wait(
    github_clock, github_stop, delay
):
    options = {} if delay is None else {"request_delay_ms": delay}
    client = GitHub(
        Credentials({"first": "fictional-secret"}), stop=github_stop, **options
    )
    starts = []

    def respond(*args, **kwargs):
        starts.append(github_clock.elapsed)
        github_clock.elapsed += 3
        return api_response({"login": "fictional"})

    client.opener.open = Mock(side_effect=respond)
    client.request("/user", "first")
    assert starts == [0] and github_stop.waits == []
    # Time spent processing other work counts towards the interval too.
    github_clock.elapsed += 0.00025
    client.request("/user", "first")
    seconds = (1000 if delay is None else delay) / 1000
    assert starts[1] == pytest.approx(3 + max(0.00025, seconds))
    assert sum(github_stop.waits) == pytest.approx(max(0, seconds - 0.00025))


def test_delay_shared_across_providers_pages_and_identity_calls(
    github_clock, github_stop
):
    client = GitHub(
        Credentials({"first": "fictional-first", "second": "fictional-second"}),
        request_delay_ms=250,
        stop=github_stop,
    )
    link = '<https://api.github.com/user/repos?per_page=100&page=2>; rel="next"'
    responses = [
        api_response({"login": "fictional"}),
        api_response([raw_repo()], {"Link": link}),
        api_response([]),
        api_response({"login": "fictional"}),
        api_response([]),
        api_response(raw_repo()),
    ]
    client.opener.open = Mock(side_effect=responses)
    assert len(client.discover(Provider("one", "first"))) == 1
    assert client.discover(Provider("two", "second")) == []
    client.request("/repos/fictional/project", "second")
    assert github_stop.waits == [0.25] * 5
    assert github_clock.elapsed == 1.25
    assert [
        c.args[0].get_header("Authorization") for c in client.opener.open.call_args_list
    ] == ["Bearer fictional-first"] * 3 + ["Bearer fictional-second"] * 3


def test_shutdown_during_delay_stops_before_next_request(github_clock, github_stop):
    client = GitHub(Credentials({"first": "fictional-secret"}), stop=github_stop)
    client.opener.open = Mock(return_value=api_response({}))
    client.request("/user", "first")
    github_stop.on_wait = lambda: setattr(github_stop, "stopped", True)
    with pytest.raises(DiscoveryError, match="INTERRUPTED"):
        client.request("/user", "first")
    assert github_stop.waits == [1]
    assert client.opener.open.call_count == 1


def test_shutdown_before_first_request_does_not_wait_or_call(github_stop):
    github_stop.stopped = True
    client = GitHub(Credentials({"first": "fictional-secret"}), stop=github_stop)
    client.opener.open = Mock()
    with pytest.raises(DiscoveryError, match="INTERRUPTED"):
        client.request("/user", "first")
    client.opener.open.assert_not_called()
    assert github_stop.waits == []


def test_delay_cannot_start_request_at_deadline(github_clock, github_stop):
    client = GitHub(
        Credentials({"first": "fictional-secret"}),
        request_delay_ms=60000,
        stop=github_stop,
    )
    client.opener.open = Mock(return_value=api_response({}))
    client.request("/user", "first")
    github_clock.elapsed = 119
    client.last_completed = 118
    with pytest.raises(DiscoveryError, match="TIMEOUT"):
        client.request("/user", "first")
    assert github_stop.waits == [1]
    assert github_clock.elapsed == 120
    assert client.opener.open.call_count == 1


def test_request_timeout_is_bounded_by_remaining_deadline(github_clock, github_stop):
    client = GitHub(Credentials({"first": "fictional-secret"}), stop=github_stop)
    github_clock.elapsed = 119.5
    client.opener.open = Mock(return_value=api_response({}))
    client.request("/user", "first")
    assert client.opener.open.call_args.kwargs["timeout"] == 0.5


@pytest.mark.parametrize("delay", [0, 250, 1000, 5000])
def test_cooldown_precedes_standard_delay_without_blocking(
    github_clock, github_stop, delay
):
    client = GitHub(
        Credentials({"first": "fictional-secret"}),
        request_delay_ms=delay,
        stop=github_stop,
    )
    until = github_clock.time() + 172800
    headers = {"retry-after": "120", "x-ratelimit-reset": str(until)}
    client.opener.open = Mock(side_effect=HTTPError("", 429, "", headers, io.BytesIO()))
    with pytest.raises(DiscoveryError, match="RATE_LIMIT") as error:
        client.request("/user", "first")
    assert error.value.retry_at == until
    github_clock.elapsed += 30
    with pytest.raises(DiscoveryError, match="RATE_LIMIT") as error:
        client.request("/user", "first")
    assert error.value.retry_at == until
    assert client.opener.open.call_count == 1
    assert github_stop.waits == []


def test_cooldown_survives_error_response_close_failure(github_clock):
    body = Mock(closed=False)
    body.close.side_effect = IncompleteRead(b"fictional-secret")
    client = GitHub(Credentials({"first": "fictional-secret"}))
    client.opener.open = Mock(
        side_effect=HTTPError("", 429, "", {"Retry-After": "172800"}, body)
    )
    with pytest.raises(DiscoveryError) as error:
        client.request("/user", "first")
    assert error.value.retry_at == github_clock.time() + 172800
    assert "fictional-secret" not in str(error.value)


def test_programming_errors_are_not_swallowed():
    client = GitHub(Credentials({"first": "fictional-secret"}))
    client.opener.open = Mock(side_effect=RuntimeError("injected programming error"))
    with pytest.raises(RuntimeError, match="programming error"):
        client.request("/user", "first")


def test_concurrent_requests_are_serialized_through_the_delay(
    github_clock, github_stop
):
    client = GitHub(Credentials({"first": "fictional-secret"}), stop=github_stop)
    entered, release, second_started = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    starts, failures = [], []

    def respond(*args, **kwargs):
        starts.append(github_clock.elapsed)
        if len(starts) == 1:
            entered.set()
            assert release.wait(5)
        return api_response({})

    def request(second=False):
        try:
            if second:
                second_started.set()
            client.request("/user", "first")
        except Exception as error:
            failures.append(error)

    client.opener.open = Mock(side_effect=respond)
    first = threading.Thread(target=request)
    second = threading.Thread(target=request, args=(True,))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert second_started.wait(5)
        assert starts == [0]
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive() and not failures
    assert starts == [0, 1] and github_stop.waits == [1]


def test_http_date_and_reset_choose_later_time(github_clock):
    date = format_datetime(
        datetime.fromtimestamp(github_clock.time() + 172800, UTC), usegmt=True
    )
    assert (
        retry_time(
            {"Retry-After": date, "X-RateLimit-Reset": str(github_clock.time() + 3600)},
            github_clock.time(),
        )
        == github_clock.time() + 172800
    )
