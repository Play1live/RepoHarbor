"""Validated PR-01 configuration. Unsupported settings fail closed."""

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from repoharbor.errors import HarborError
from repoharbor.scheduler import Cron


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_\-]{0,63}", value):
        raise HarborError("Invalid identifier; use lowercase letters, digits, _ or -")
    return value


def canonical_url(value: Any) -> str:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise HarborError("Invalid HTTPS URL")
    try:
        u = urlsplit(value)
        port = u.port
        host = u.hostname
    except ValueError:
        raise HarborError("Invalid HTTPS URL") from None
    if (
        u.scheme != "https"
        or not host
        or u.username is not None
        or u.password is not None
        or u.query
        or u.fragment
        or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)
        or any(p in (".", "..") for p in unquote(u.path).split("/"))
        or "\\" in unquote(u.path)
        or "%" in unquote(u.path)
    ):
        raise HarborError("Only credential-free HTTPS URLs without query are supported")
    authority = host.lower() + (f":{port}" if port and port != 443 else "")
    return urlunsplit(("https", authority, u.path.rstrip("/"), "", ""))


def keys(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise HarborError("Unknown or malformed configuration field")
    return value


def gib(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise HarborError("Storage limits must be nonnegative GiB or null")
    return int(Decimal(str(value)) * 1024**3)


@dataclass(frozen=True)
class Source:
    alias: str
    url: str
    web_url: str | None = None
    credential: str | None = None


@dataclass(frozen=True)
class Storage:
    global_bytes: int | None = None
    repo_bytes: int | None = None
    overrides: dict[str, int | None] = field(default_factory=dict)
    during_transfer: str = "finish"
    min_free_bytes: int = 2 * 1024**3


@dataclass(frozen=True)
class Config:
    sources: tuple[Source, ...]
    credentials: tuple[str, ...]
    token_source: str
    schedule: str = "0 3 * * *"
    timezone: str = "UTC"
    root: Path = Path("/backups")
    secrets_dir: Path = Path("/run/secrets/repoharbor")
    storage: Storage = field(default_factory=Storage)
    automatic_repair: bool = True
    repair_attempts: int = 3


def load(env: dict[str, str]) -> Config:
    mode = env.get("REPOHARBOR_TOKEN_SOURCE")
    if mode not in ("env", "file"):
        raise HarborError("REPOHARBOR_TOKEN_SOURCE must explicitly be env or file")
    try:
        data = yaml.safe_load(env.get("REPOHARBOR_CONFIG", "{}"))
    except yaml.YAMLError:
        raise HarborError("Invalid configuration YAML") from None
    d = keys(
        data,
        {
            "schema_version",
            "schedule",
            "credentials",
            "manual_git_urls",
            "storage",
            "recovery",
            "features",
        },
    )
    if type(d.get("schema_version")) is not int or d["schema_version"] != 1:
        raise HarborError("schema_version must be 1")
    schedule = d.get("schedule", "0 3 * * *")
    Cron(schedule)
    timezone = env.get("TZ", "UTC")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HarborError("Invalid IANA timezone") from None
    names = d.get("credentials", [])
    if not isinstance(names, list):
        raise HarborError("credentials must be a list")
    names = [identifier(n) for n in names]
    # Both '-' and '_' map to '_' in environment variable names.
    if len({n.upper().replace("-", "_") for n in names}) != len(names):
        raise HarborError("Duplicate credential names")
    raw_sources = d.get("manual_git_urls", [])
    if not isinstance(raw_sources, list):
        raise HarborError("manual_git_urls must be a list")
    sources: list[Source] = []
    for raw in raw_sources:
        s = keys(raw, {"alias", "url", "web_url", "credential"})
        alias, url = identifier(s.get("alias")), canonical_url(s.get("url"))
        credential = s.get("credential")
        if credential is not None and credential not in names:
            raise HarborError("Unknown credential reference")
        if any(x.alias == alias for x in sources):
            raise HarborError("Duplicate source alias")
        existing = next((x for x in sources if x.url == url), None)
        if existing:
            if existing.credential != credential:
                raise HarborError("Duplicate URL with conflicting credentials")
            continue
        sources.append(
            Source(
                alias,
                url,
                canonical_url(s["web_url"]) if s.get("web_url") is not None else None,
                credential,
            )
        )
    st = keys(
        d.get("storage", {}),
        {
            "global_gib",
            "default_repo_gib",
            "overrides",
            "during_transfer",
            "min_free_gib",
        },
    )
    overrides = st.get("overrides", {})
    if not isinstance(overrides, dict) or set(overrides) - {s.alias for s in sources}:
        raise HarborError("Storage overrides must reference source aliases")
    transfer = st.get("during_transfer", "finish")
    reserve = gib(st.get("min_free_gib", 2))
    if transfer not in ("finish", "abort") or reserve is None:
        raise HarborError("Invalid transfer mode or free-space reserve")
    storage = Storage(
        gib(st.get("global_gib")),
        gib(st.get("default_repo_gib")),
        {k: gib(v) for k, v in overrides.items()},
        transfer,
        reserve,
    )
    recovery = keys(
        d.get("recovery", {}), {"automatic_repair", "max_repair_attempts_per_incident"}
    )
    repair = recovery.get("automatic_repair", True)
    attempts = recovery.get("max_repair_attempts_per_incident", 3)
    if type(repair) is not bool or type(attempts) is not int or not 1 <= attempts <= 3:
        raise HarborError("Invalid repair settings")
    features = keys(
        d.get("features", {}),
        {
            "web_ui",
            "integrity_check",
            "auto_retry",
            "manual_run",
            "restore_helper",
            "extended_logging",
        },
    )
    if any(v is not False for v in features.values()):
        raise HarborError("Optional features are not available in PR 01")
    root = Path(env.get("REPOHARBOR_BACKUP_ROOT", "/backups"))
    secrets = Path(env.get("REPOHARBOR_SECRETS_DIR", "/run/secrets/repoharbor"))
    if not root.is_absolute() or not secrets.is_absolute():
        raise HarborError("Backup and secret paths must be absolute")
    return Config(
        tuple(sources),
        tuple(names),
        mode,
        schedule,
        timezone,
        root,
        secrets,
        storage,
        repair,
        attempts,
    )
