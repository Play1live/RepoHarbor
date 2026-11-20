import itertools
from dataclasses import replace

import pytest
import yaml

from repoharbor.config import canonical_url, gib, load
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError


def env(data=None, **extra):
    return {
        "REPOHARBOR_TOKEN_SOURCE": "env",
        "REPOHARBOR_CONFIG": yaml.safe_dump({"schema_version": 1, **(data or {})}),
        **extra,
    }


def test_defaults_and_supported_schema():
    cfg = load(env())
    assert cfg.schedule == "0 3 * * *" and cfg.sources == ()
    cfg = load(
        env(
            {
                "manual_git_urls": [
                    {
                        "alias": "first",
                        "url": "https://EXAMPLE.org:443/repo.git/",
                        "web_url": "https://example.org/repo",
                    },
                    {"alias": "second", "url": "https://example.org/repo.git"},
                ],
                "storage": {
                    "overrides": {"first": None},
                    "global_gib": 1.5,
                    "default_repo_gib": 0,
                },
                "features": {"web_ui": False},
                "recovery": {
                    "automatic_repair": False,
                    "max_repair_attempts_per_incident": 1,
                },
            },
            TZ="Europe/Berlin",
        )
    )
    assert len(cfg.sources) == 1 and cfg.sources[0].alias == "first"
    assert cfg.storage.global_bytes == 1610612736 and cfg.storage.repo_bytes == 0
    assert cfg.timezone == "Europe/Berlin" and not cfg.automatic_repair
    assert (
        canonical_url("https://example.org:8443/r.git")
        == "https://example.org:8443/r.git"
    )


@pytest.mark.parametrize(
    "data",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"providers": [{}]},
        {"credentials": "demo"},
        {"credentials": ["../bad"]},
        {"credentials": ["my-token", "my_token"]},
        {"manual_git_urls": {}},
        {
            "manual_git_urls": [
                {
                    "alias": "demo",
                    "url": "https://example.org/r",
                    "credential": "unknown",
                }
            ]
        },
        {"manual_git_urls": [{"alias": "demo", "url": "https://example.org/r"}] * 2},
        {
            "credentials": ["one", "two"],
            "manual_git_urls": [
                {"alias": "a", "url": "https://example.org/r", "credential": "one"},
                {"alias": "b", "url": "https://example.org/r", "credential": "two"},
            ],
        },
        {"storage": {"overrides": []}},
        {"storage": {"overrides": {"unknown": 1}}},
        {"storage": {"during_transfer": "unknown"}},
        {"storage": {"min_free_gib": None}},
        {"recovery": {"automatic_repair": "true"}},
        {"recovery": {"max_repair_attempts_per_incident": 4}},
        {"features": {"web_ui": True}},
        {"features": {"manual_run": 0}},
        {"features": {"unknown": False}},
        {"schedule": "bad"},
    ],
)
def test_invalid_config(data):
    with pytest.raises(HarborError):
        load(env(data))


@pytest.mark.parametrize(
    "extra",
    [
        {"REPOHARBOR_TOKEN_SOURCE": "mixed"},
        {"TZ": "Not/AZone"},
        {"REPOHARBOR_CONFIG": "[bad"},
        {"REPOHARBOR_CONFIG": "[]"},
        {"REPOHARBOR_BACKUP_ROOT": "relative"},
        {"REPOHARBOR_SECRETS_DIR": "relative"},
    ],
)
def test_invalid_environment(extra):
    with pytest.raises(HarborError):
        load(env(**extra))


@pytest.mark.parametrize(
    "value",
    [
        None,
        "http://example.org/r",
        "file:///tmp/r",
        "ext::touch /tmp/x",
        "https://token@example.org/r",
        "https://example.org/r?q=token",
        "https://example.org/r#token",
        "https://example.org/../r",
        "https://example.org/%2e%2e/r",
        "https://example.org/r%255c",
        "https://example.org/a\\b",
        "https://example.org:bad/r",
        "https://[bad",
        "https://example.org/r\n",
        "https://example_org/r",
    ],
)
def test_unsafe_urls(value):
    with pytest.raises(HarborError):
        canonical_url(value)


@pytest.mark.parametrize("value", [-1, True, "2", float("inf"), float("nan")])
def test_invalid_limit(value):
    with pytest.raises(HarborError):
        gib(value)


def test_credential_modes_rotation_and_redaction(tmp_path):
    cfg = replace(load(env({"credentials": ["demo", "other"]})), secrets_dir=tmp_path)
    values = {
        "REPOHARBOR_TOKEN_DEMO": "fictional-one",
        "REPOHARBOR_TOKEN_OTHER": "fictional-two",
    }
    creds = Credentials.resolve(cfg, values)
    assert "fictional" not in repr(creds)
    assert creds.git_env("demo")["REPOHARBOR_ASKPASS_TOKEN"] == "fictional-one"
    assert "REPOHARBOR_ASKPASS_TOKEN" not in creds.git_env(None)
    for name in cfg.credentials:
        (tmp_path / name).write_text("rotated-token\n")
    with pytest.raises(HarborError, match="Mixed"):
        Credentials.resolve(cfg, values)
    cfg = replace(cfg, token_source="file")
    with pytest.raises(HarborError, match="Mixed"):
        Credentials.resolve(cfg, values)
    assert Credentials.resolve(cfg, {}).values == {
        "demo": "rotated-token",
        "other": "rotated-token",
    }
    (tmp_path / "demo").write_text("new-token")
    assert Credentials.resolve(cfg, {}).values["demo"] == "new-token"
    (tmp_path / "demo").unlink()
    with pytest.raises(HarborError, match="Missing"):
        Credentials.resolve(cfg, {})
    (tmp_path / "demo").symlink_to(tmp_path / "other")
    with pytest.raises(HarborError):
        Credentials.resolve(cfg, {})


@pytest.mark.parametrize("value", ["", "bad\nvalue", "bad\x00value"])
def test_bad_env_secret(tmp_path, value):
    cfg = replace(load(env({"credentials": ["demo"]})), secrets_dir=tmp_path)
    with pytest.raises(HarborError):
        Credentials.resolve(cfg, {"REPOHARBOR_TOKEN_DEMO": value})


@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("restore", [True, False])
def test_restore_feature_configuration(manual, restore):
    config = load(env({"features": {"manual_run": manual, "restore_helper": restore}}))
    assert config.manual_run is manual
    assert config.restore_helper is restore
    assert load(env()).restore_helper is False


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
def test_restore_feature_requires_boolean(value):
    with pytest.raises(HarborError, match="unsupported optional feature"):
        load(env({"features": {"restore_helper": value}}))


def test_restore_does_not_enable_unsupported_dashboard():
    with pytest.raises(HarborError, match="unsupported optional feature"):
        load(env({"features": {"restore_helper": True, "web_ui": True}}))


@pytest.mark.parametrize(
    "enabled,manual,restore", itertools.product([False, True], repeat=3)
)
def test_integrity_feature_is_independent(enabled, manual, restore):
    config = load(
        env(
            {
                "features": {
                    "integrity_check": enabled,
                    "manual_run": manual,
                    "restore_helper": restore,
                }
            }
        )
    )
    assert (config.integrity_check, config.manual_run, config.restore_helper) == (
        enabled,
        manual,
        restore,
    )
    assert config.integrity.schedule == "0 4 * * 0"
    assert config.integrity.include_excluded is True
    assert not load(env()).integrity_check


@pytest.mark.parametrize(
    "block",
    [
        None,
        [],
        {"unexpected": True},
        {"include_excluded": "false"},
        {"include_excluded": 1},
        {"schedule": "* * * *"},
        {"schedule": "60 * * * *"},
        {"schedule": False},
    ],
)
def test_integrity_block_validated_even_when_disabled(block):
    with pytest.raises(HarborError):
        load(env({"integrity": block}))


@pytest.mark.parametrize("flag", ["true", "false", 0, 1, None, [], {}])
def test_integrity_flag_requires_boolean(flag):
    with pytest.raises(HarborError):
        load(env({"features": {"integrity_check": flag}}))


def test_integrity_custom_schedule_and_exclusions():
    config = load(
        env(
            {
                "schedule": "3 * * * *",
                "integrity": {
                    "schedule": "5 * * * *",
                    "include_excluded": False,
                },
            }
        )
    )
    assert config.schedule == "3 * * * *"
    assert config.integrity.schedule == "5 * * * *"
    assert config.integrity.include_excluded is False


@pytest.mark.parametrize(
    "retry",
    [{"max_retries": value} for value in (False, True, 0, 11, "3", 1.5, None)]
    + [{"initial_delay_seconds": value} for value in (False, 0, -1, 3601, "300")]
    + [{"max_delay_seconds": value} for value in (True, 299, 86401, 3600.0)]
    + [{"unknown": 1}, None],
)
def test_invalid_retry_config_rejected_when_disabled(retry):
    with pytest.raises(HarborError):
        load(env({"retry": retry}))


@pytest.mark.parametrize("value", [0, 1, "true", None, []])
def test_retry_feature_requires_boolean(value):
    with pytest.raises(HarborError, match="optional feature"):
        load(env({"features": {"auto_retry": value}}))


def test_retry_defaults_and_limits():
    config = load(env({}))
    assert not config.auto_retry and config.retry.max_retries == 3
    assert [config.retry.delay(n) for n in range(3)] == [300, 600, 1200]
    config = load(
        env(
            {
                "features": {"auto_retry": True},
                "retry": {
                    "max_retries": 10,
                    "initial_delay_seconds": 1,
                    "max_delay_seconds": 3,
                },
            }
        )
    )
    assert config.auto_retry and not config.manual_run
    assert [config.retry.delay(n) for n in range(4)] == [1, 2, 3, 3]
