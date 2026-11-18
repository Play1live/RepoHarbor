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
from repoharbor.selection import Selection, instance_host, repository_key, selector


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
class Provider:
    id: str
    credential: str
    include_owned: bool = True
    include_organizations: bool = True
    include_collaborators: bool = True
    type: str = "github"
    host: str = "github.com"
    include_groups: bool = True
    include_shared: bool = True


@dataclass(frozen=True)
class Discovery:
    request_delay_ms: int = 1000


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
    providers: tuple[Provider, ...] = ()
    selection: Selection = field(default_factory=Selection)
    discovery: Discovery = field(default_factory=Discovery)


def discovery_settings(
    d: dict[str, Any], names: list[str]
) -> tuple[tuple[Provider, ...], Selection]:
    raw = d.get("providers", [])
    if not isinstance(raw, list):
        raise HarborError("providers must be a list")
    providers: list[Provider] = []
    for value in raw:
        p = keys(
            value,
            {
                "id",
                "type",
                "host",
                "credential",
                "include_owned",
                "include_organizations",
                "include_collaborators",
                "include_groups",
                "include_shared",
            },
        )
        name = identifier(p.get("id"))
        credential = p.get("credential")
        kind = p.get("type")
        host = p.get("host", "github.com" if kind == "github" else "gitlab.com")
        if kind not in ("github", "gitlab"):
            raise HarborError("Unknown discovery provider")
        instance_host(host)
        if kind == "github" and (
            host != "github.com" or {"include_groups", "include_shared"}.intersection(p)
        ):
            raise HarborError("Invalid GitHub provider")
        if kind == "gitlab" and (
            host in ("github.com", "api.github.com")
            or {"include_organizations", "include_collaborators"}.intersection(p)
        ):
            raise HarborError("Invalid GitLab provider")
        if credential not in names or any(x.id == name for x in providers):
            raise HarborError("Invalid provider credential or duplicate provider ID")
        flags = [
            p.get(k, True)
            for k in ("include_owned", "include_organizations", "include_collaborators")
        ]
        if any(type(x) is not bool for x in flags):
            raise HarborError("Provider scope flags must be booleans")
        groups, shared = p.get("include_groups", True), p.get("include_shared", True)
        if type(groups) is not bool or type(shared) is not bool:
            raise HarborError("Provider scope flags must be booleans")
        # A credential belongs to exactly one service authority; no cross-host reuse.
        if any(
            x.credential == credential and (x.type, x.host) != (kind, host)
            for x in providers
        ):
            raise HarborError("Credential is already bound to another provider host")
        providers.append(
            Provider(
                name,
                credential,
                flags[0],
                flags[1],
                flags[2],
                kind,
                host,
                groups,
                shared,
            )
        )
    s = keys(
        d.get("selection", {}),
        {
            "mode",
            "visibility",
            "include_forks",
            "include_archived",
            "selected",
            "excluded",
        },
    )
    mode, visibility = (
        s.get("mode", "selected"),
        s.get("visibility", ["public", "private"]),
    )
    if (
        mode not in ("all", "selected", "all_except")
        or not isinstance(visibility, list)
        or not visibility
        or any(v not in ("public", "private", "internal") for v in visibility)
    ):
        raise HarborError("Invalid selection mode or visibility")
    forks, archived = s.get("include_forks", False), s.get("include_archived", False)
    if type(forks) is not bool or type(archived) is not bool:
        raise HarborError("Selection flags must be booleans")
    lists = []
    for key in ("selected", "excluded"):
        values = s.get(key, [])
        if not isinstance(values, list):
            raise HarborError("Repository selectors must be lists")
        lists.append(tuple(selector(v) for v in values))
    return tuple(providers), Selection(mode, tuple(visibility), forks, archived, *lists)


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
            "providers",
            "selection",
            "discovery",
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
    providers, selection = discovery_settings(d, names)
    discovery = keys(d.get("discovery", {}), {"request_delay_ms"})
    delay = discovery.get("request_delay_ms", 1000)
    if type(delay) is not int or not 0 <= delay <= 60000:
        raise HarborError(
            "discovery.request_delay_ms must be an integer from 0 to 60000"
        )
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
        bound = next((p for p in providers if p.credential == credential), None)
        if bound is not None and urlsplit(url).netloc != bound.host:
            raise HarborError("Manual source credential belongs to another host")
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
    if not isinstance(overrides, dict):
        raise HarborError("Storage overrides must be a mapping")
    for key in overrides:
        if key not in {s.alias for s in sources}:
            repository_key(key)
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
        providers,
        selection,
        Discovery(delay),
    )
