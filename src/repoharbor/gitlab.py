"""GitLab v4 discovery bounded to an explicitly configured HTTPS authority."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Mapping
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from repoharbor import __version__
from repoharbor.config import Provider
from repoharbor.errors import HarborError
from repoharbor.github import (
    DiscoveryError,
    GitHub,
    normalized_headers,
    retry_time,
)
from repoharbor.providers import Observation, Repository
from repoharbor.selection import address, instance_host, repository_key


def quota_exhausted(headers: Mapping[str, str] | Message[str, str]) -> bool:
    return any(
        name == "ratelimit-remaining" and re.fullmatch(r"0+", value)
        for name, value in normalized_headers(headers)
    )


def cooldown(
    headers: Mapping[str, str] | Message[str, str],
    now: float,
    *,
    require_reset: bool = False,
) -> float:
    translated = Message()
    dates = []
    for name, value in normalized_headers(headers):
        if name == "ratelimit-resettime":
            with contextlib.suppress(ValueError, TypeError, OverflowError):
                date = parsedate_to_datetime(value)
                if date.tzinfo is not None and date.timestamp() > now:
                    dates.append(date.timestamp())
        elif name in ("retry-after", "ratelimit-reset"):
            translated["x-ratelimit-reset" if name == "ratelimit-reset" else name] = (
                value
            )
    return max(
        [
            retry_time(translated, now, require_reset=require_reset and not dates),
            *dates,
        ]
    )


def numeric_id(value: Any) -> int:
    if type(value) is not int or not 0 < value < 10**20:
        raise DiscoveryError("GITLAB_INVALID_ID")
    return value


def parse_project(value: Any, host: str) -> Repository:
    try:
        instance_host(host)
        rid = numeric_id(value["id"])
        namespace = value["namespace"]
        numeric_id(namespace["id"])
        owner, name = namespace["full_path"], value["path"]
        if not isinstance(owner, str) or not isinstance(name, str):
            raise ValueError
        if namespace["kind"] not in ("user", "group"):
            raise ValueError
        if namespace["kind"] == "user" and "/" in owner:
            raise ValueError
        address(f"{host}/{owner}/{name}")
        if value["path_with_namespace"] != f"{owner}/{name}":
            raise ValueError
        if "/" in name:
            raise ValueError
        url = f"https://{host}/{owner}/{name}"
        if value["http_url_to_repo"] != url + ".git" or value["web_url"] != url:
            raise ValueError
        visibility, archived = value["visibility"], value["archived"]
        if (
            visibility not in ("public", "internal", "private")
            or type(archived) is not bool
        ):
            raise ValueError
        upstream = value.get("forked_from_project")
        fork_marker = value.get("mr_default_target_self")
        if "mr_default_target_self" in value and type(fork_marker) is not bool:
            raise ValueError
        fork = upstream is not None or "mr_default_target_self" in value
        if upstream is not None:
            numeric_id(value["forked_from_project"]["id"])
        branch, created = value["default_branch"], value["created_at"]
        if branch is not None and (
            not isinstance(branch, str)
            or len(branch) > 255
            or any(ord(c) < 32 for c in branch)
        ):
            raise ValueError
        if not isinstance(created, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", created
        ):
            raise ValueError
        return Repository(
            repository_key(f"gitlab:{host}:{rid}"),
            owner,
            name,
            visibility,
            fork,
            archived,
            branch,
            created,
        )
    except (KeyError, TypeError, ValueError, AttributeError, HarborError):
        raise DiscoveryError("GITLAB_INVALID_PROJECT") from None


class GitLab(GitHub):
    quota_exhausted = staticmethod(quota_exhausted)
    retry_time = staticmethod(cooldown)

    def bind(self, host: str) -> None:
        self.host = instance_host(host)
        self.base = f"https://{self.host}/api/v4"
        self.pages = 0
        self.on_cooldown: Callable[[float], None] = lambda until: None

    def persist_cooldown(self) -> None:
        self.on_cooldown(self.retry_at)

    def headers(self, credential: str) -> dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.credentials.values[credential],
            "Accept": "application/json",
            "User-Agent": f"RepoHarbor/{__version__}",
        }

    def request(self, path: str, credential: str) -> tuple[Any, Mapping[str, str]]:
        # All callers construct relative, endpoint-specific paths locally.
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise DiscoveryError("GITLAB_INVALID_ENDPOINT")
        try:
            return super().request(path, credential)
        except DiscoveryError as error:
            raise DiscoveryError(
                str(error).replace("GITHUB_", "GITLAB_"), error.retry_at
            ) from None

    def listing(
        self, endpoint: str, params: dict[str, str], credential: str
    ) -> list[Any]:
        """Offset pagination for groups; keyset for projects, without trusting links."""
        keyset = endpoint == "/projects"
        fixed = {"per_page": "100", "order_by": "id", "sort": "asc", **params}
        if keyset:
            fixed["pagination"] = "keyset"
        cursor = {"id_after": "0"} if keyset else {"page": "1"}
        result = []
        last_id = 0
        while self.pages < 1000:
            self.pages += 1
            path = endpoint + "?" + urlencode(fixed | cursor)
            values, raw_headers = self.request(path, credential)
            headers = dict(normalized_headers(raw_headers))
            if not isinstance(values, list) or len(values) > 100:
                raise DiscoveryError("GITLAB_INVALID_PAGE")
            try:
                ids = [numeric_id(v["id"]) for v in values]
            except (KeyError, TypeError):
                raise DiscoveryError("GITLAB_INVALID_PAGE") from None
            if ids and (ids != sorted(set(ids)) or ids[0] <= last_id):
                raise DiscoveryError("GITLAB_INCONSISTENT_PAGINATION")
            if ids:
                last_id = ids[-1]
            result.extend(values)
            next_cursor = None
            link = headers.get("link", "")
            pattern = r'<([^>]+)>;\s*rel="(next|last|first|prev)"'
            links = re.findall(pattern, link)
            if re.sub(pattern, "", link).strip(" ,") or len(
                {r for _, r in links}
            ) != len(links):
                raise DiscoveryError("GITLAB_INVALID_PAGINATION")
            for url, relation in links:
                try:
                    u = urlsplit(url)
                    q = parse_qs(u.query, keep_blank_values=True)
                except ValueError:
                    raise DiscoveryError("GITLAB_INVALID_PAGINATION") from None
                cursor_key = "id_after" if keyset else "page"
                if (
                    u.scheme != "https"
                    or u.netloc != self.host
                    or u.path != "/api/v4" + endpoint
                    or u.fragment
                    or set(q) != set(fixed) | {cursor_key}
                    or any(q.get(k) != [v] for k, v in fixed.items())
                    or len(q[cursor_key]) != 1
                    or not re.fullmatch(r"[0-9]{1,20}", q[cursor_key][0])
                ):
                    raise DiscoveryError("GITLAB_INVALID_PAGINATION")
                if relation == "next":
                    n = q[cursor_key][0]
                    if (
                        int(n) <= int(cursor[cursor_key])
                        or (keyset and int(n) != last_id)
                        or (not keyset and int(n) != int(cursor["page"]) + 1)
                    ):
                        raise DiscoveryError("GITLAB_INVALID_PAGINATION")
                    next_cursor = {cursor_key: n}
            if not keyset:
                page = int(cursor["page"])
                if "x-page" in headers and headers["x-page"] != str(page):
                    raise DiscoveryError("GITLAB_INVALID_PAGINATION")
                n = headers.get("x-next-page", "")
                if n:
                    if n != str(page + 1) or (
                        next_cursor and next_cursor != {"page": n}
                    ):
                        raise DiscoveryError("GITLAB_INVALID_PAGINATION")
                    next_cursor = {"page": n}
                total = headers.get("x-total-pages")
                if total is not None and (not total.isdigit() or len(total) > 6):
                    raise DiscoveryError("GITLAB_INVALID_PAGINATION")
                if total and int(total) > page and not next_cursor:
                    raise DiscoveryError("GITLAB_INCOMPLETE_PAGINATION")
            if next_cursor:
                if not values:
                    raise DiscoveryError("GITLAB_INCOMPLETE_PAGINATION")
                cursor = next_cursor
            elif len(values) == 100:
                # Request a locally constructed continuation to prove the end,
                # including when pagination headers are absent at exact multiples.
                cursor = (
                    {"id_after": str(last_id)}
                    if keyset
                    else {"page": str(int(cursor["page"]) + 1)}
                )
            else:
                return result
        raise DiscoveryError("GITLAB_PAGINATION_LIMIT")

    def discover(self, provider: Provider) -> list[Observation]:
        user, _ = self.request("/user", provider.credential)
        try:
            numeric_id(user["id"])
            username = user["username"]
            if not isinstance(username, str):
                raise ValueError
            if user.get("bot", False) is not False:
                raise ValueError
        except (KeyError, TypeError, AttributeError, ValueError):
            raise DiscoveryError("GITLAB_UNSUPPORTED_USER") from None
        # Membership avoids enumerating unrelated public projects.
        raw = self.listing("/projects", {"membership": "true"}, provider.credential)
        raw += self.listing("/projects", {"owned": "true"}, provider.credential)
        groups = self.listing(
            "/groups", {"all_available": "false"}, provider.credential
        )
        group_projects: set[str] = set()
        for group in groups:
            try:
                gid = numeric_id(group["id"])
            except (KeyError, TypeError):
                raise DiscoveryError("GITLAB_INVALID_GROUP") from None
            for shared in ("false", "true"):
                projects = self.listing(
                    f"/groups/{gid}/projects",
                    {"include_subgroups": "true", "with_shared": shared},
                    provider.credential,
                )
                raw.extend(projects)
                if shared == "false":
                    group_projects.update(
                        parse_project(p, self.host).key for p in projects
                    )
        found: dict[str, Observation] = {}
        for value in raw:
            repo = parse_project(value, self.host)
            personal = value["namespace"]["kind"] == "user" and repo.owner == username
            scope = (
                provider.include_owned
                if personal
                else provider.include_groups
                if repo.key in group_projects
                else provider.include_shared
            )
            obs = Observation(repo, provider.credential, scope)
            if repo.key in found and found[repo.key] != obs:
                raise DiscoveryError("GITLAB_INCONSISTENT_ID")
            found[repo.key] = obs
        return list(found.values())

    def manual_identity(self, name: str, credential: str) -> str:
        project = name.removeprefix(self.host + "/")
        raw, _ = self.request("/projects/" + quote(project, safe=""), credential)
        return parse_project(raw, self.host).key
