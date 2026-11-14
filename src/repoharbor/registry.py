"""Conservative identity reconciliation, without moving or adopting mirrors."""

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from repoharbor.config import Config, Source
from repoharbor.errors import HarborError
from repoharbor.github import DiscoveryError, GitHub, parse_repository
from repoharbor.gitlab import parse_project
from repoharbor.persistence import metadata, utcnow
from repoharbor.providers import Observation, Repository
from repoharbor.selection import address, repository_key


def stored_repository(meta: dict[str, Any], key: str) -> Repository:
    r = meta["repository"]
    if r["provider"] == "gitlab":
        if type(r["fork"]) is not bool:
            raise DiscoveryError("GITLAB_INVALID_STORED_PROJECT")
        if repository_key(f"gitlab:{r['host']}:{r['id']}") != key:
            raise DiscoveryError("GITLAB_INVALID_STORED_IDENTITY")
        repo = parse_project(
            {
                "id": int(r["id"]),
                "namespace": {"id": 1, "kind": "group", "full_path": r["owner"]},
                "path": r["name"],
                "path_with_namespace": f"{r['owner']}/{r['name']}",
                "visibility": r["visibility"],
                "archived": r["archived"],
                "forked_from_project": {"id": 1} if r["fork"] else None,
                "default_branch": r["default_branch"],
                "created_at": r["source_created_at"],
                "http_url_to_repo": r["clone_url"],
                "web_url": r["web_url"],
            },
            r["host"],
        )
    else:
        if (
            r["provider"] != "github"
            or r["host"] != "github.com"
            or repository_key(f"github:github.com:{r['id']}") != key
        ):
            raise DiscoveryError("GITHUB_INVALID_STORED_IDENTITY")
        repo = parse_repository(
            {
                "id": int(r["id"]),
                "owner": {"login": r["owner"], "type": "User"},
                "name": r["name"],
                "full_name": f"{r['owner']}/{r['name']}",
                "private": r["visibility"] == "private",
                "visibility": r["visibility"],
                "fork": r["fork"],
                "archived": r["archived"],
                "default_branch": r["default_branch"],
                "created_at": r["source_created_at"],
                "clone_url": r["clone_url"],
                "html_url": r["web_url"],
            }
        )
    aliases = r["aliases"]
    if (
        not isinstance(aliases, list)
        or not aliases
        or any(address(x) != x or x.split("/")[0] != repo.host for x in aliases)
        or repo.address not in aliases
    ):
        raise DiscoveryError("GITHUB_INVALID_STORED_ALIASES")
    return repo


def manual_addresses(
    config: Config, catalog: dict[str, dict[str, Any]], host: str = "github.com"
) -> set[str]:
    urls = {s.url for s in config.sources} | {
        m["repository"]["clone_url"]
        for m in catalog.values()
        if m["repository"]["provider"] == "manual"
    }
    try:
        return {address(url) for url in urls if urlsplit(url).netloc == host}
    except HarborError:
        raise DiscoveryError("GITHUB_MANUAL_IDENTITY_UNRESOLVED") from None


def manual_identities(client: GitHub, config: Config, names: set[str]) -> set[str]:
    """Resolve current manual addresses only to block overlaps, never to adopt data."""
    result = set()
    credentials = list(
        dict.fromkeys(p.credential for p in config.providers if p.type == "github")
    )
    for name in sorted(names):
        for credential in credentials:
            try:
                raw, _ = client.request(
                    "/repos/" + name.removeprefix("github.com/"), credential
                )
                result.add(parse_repository(raw).key)
                break
            except DiscoveryError as error:
                if str(error) != "GITHUB_NOT_ACCESSIBLE":
                    raise
        else:
            raise DiscoveryError("GITHUB_MANUAL_IDENTITY_UNRESOLVED")
    return result


def plan(
    config: Config,
    catalog: dict[str, dict[str, Any]],
    observations: list[Observation],
    manual_ids: set[str],
    *,
    provider_type: str = "github",
    host: str = "github.com",
) -> tuple[dict[str, dict[str, Any]], tuple[Source, ...]]:
    found: dict[str, Repository] = {}
    credentials: dict[str, str] = {}
    for item in observations:
        repo = item.repository
        if repo.provider != provider_type or repo.host != host:
            raise DiscoveryError("PROVIDER_IDENTITY_CONFLICT")
        if repo.key in found and found[repo.key] != repo:
            raise DiscoveryError("GITHUB_INCONSISTENT_ID")
        found[repo.key] = repo
        if item.in_scope:
            credentials.setdefault(repo.key, item.credential)
    proposed = {
        key: deepcopy(m)
        for key, m in catalog.items()
        if m["repository"]["provider"] == provider_type
        and m["repository"]["host"] == host
    }
    claimed: dict[str, str] = {}
    for key, meta in proposed.items():
        for name in meta["repository"]["aliases"]:
            if name in claimed and claimed[name] != key:
                raise DiscoveryError("GITHUB_ALIAS_CONFLICT")
            claimed[name] = key
    for key, observed in found.items():
        if observed.address in claimed and claimed[observed.address] != key:
            # A familiar URL may have been reassigned to another GitHub ID.
            raise DiscoveryError("GITHUB_ALIAS_CONFLICT")
        claimed[observed.address] = key
    manual = manual_addresses(config, catalog, host)
    sources = []
    for key in sorted(set(proposed) | set(found)):
        current = found.get(key)
        if key not in proposed:
            assert current is not None
            proposed[key] = metadata(key, current.url, current.url[:-4], None)
            proposed[key]["repository"] = {"aliases": []}
        meta = proposed[key]
        if current is not None:
            repo = current
            aliases = set(meta["repository"]["aliases"]) | {repo.address}
            meta["repository"].update(
                {
                    "provider": repo.provider,
                    "host": repo.host,
                    "id": key.rsplit(":", 1)[1],
                    "owner": repo.owner,
                    "name": repo.name,
                    "clone_url": repo.url,
                    "web_url": repo.url[:-4],
                    "visibility": repo.visibility,
                    "fork": repo.fork,
                    "archived": repo.archived,
                    "default_branch": repo.default_branch,
                    "source_created_at": repo.created_at,
                    "aliases": sorted(aliases),
                }
            )
            # API visibility is not evidence of successful Git access. Preserve
            # transport status, diagnostics and extensions; only observe the API.
            meta["source"].update(discovery_status="available", last_seen_at=utcnow())
        else:
            repo = stored_repository(meta, key)
            aliases = set(meta["repository"]["aliases"])
            meta["source"]["discovery_status"] = "unavailable"
        reason = config.selection.reason(
            key, aliases, repo.visibility, repo.fork, repo.archived
        )
        if not config.providers:
            reason = "provider_disabled"
        elif key in found and key not in credentials:
            reason = reason or "scope_filter"
        if key in manual_ids or aliases.intersection(manual):
            reason = "manual_identity_conflict"
        meta["selection"] = {
            "status": "excluded" if reason else "included",
            "reason": reason,
        }
        credential = credentials.get(key)
        if credential:
            meta["authentication"]["credential_name"] = credential
        if reason is None and key in found and credential:
            sources.append(Source(key, repo.url, repo.url[:-4], credential))
    return proposed, tuple(sources)
