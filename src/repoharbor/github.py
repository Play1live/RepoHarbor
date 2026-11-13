"""Bounded GitHub REST reads; no redirects or remote diagnostic disclosure."""

import contextlib
import json
import re
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from repoharbor.config import Provider
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError
from repoharbor.providers import Observation, Repository
from repoharbor.selection import address, repository_key

BASE = "https://api.github.com"
MAX_BODY = 4 * 1024 * 1024


class DiscoveryError(HarborError):
    def __init__(self, code: str, retry_at: float = 0):
        super().__init__(code)
        self.retry_at = retry_at


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def retry_time(headers: Mapping[str, str], now: float) -> float:
    delay = 60.0
    value = headers.get("Retry-After", "")
    try:
        delay = max(delay, float(value))
    except ValueError:
        with contextlib.suppress(ValueError, TypeError, OverflowError):
            delay = max(delay, parsedate_to_datetime(value).timestamp() - now)
    with contextlib.suppress(ValueError):
        delay = max(delay, float(headers.get("X-RateLimit-Reset", "0")) - now)
    # Invalid/unbounded server values cannot disable backups forever.
    return now + min(delay, 86400)


def parse_repository(value: Any) -> Repository:
    try:
        rid, owner, name = value["id"], value["owner"], value["name"]
        if type(rid) is not int or rid <= 0:
            raise ValueError
        key = repository_key(f"github:github.com:{rid}")
        login = owner["login"]
        if owner["type"] not in ("User", "Organization"):
            raise ValueError
        address(f"github.com/{login}/{name}")
        if not isinstance(login, str) or not isinstance(name, str):
            raise ValueError
        if value["full_name"].lower() != f"{login}/{name}".lower():
            raise ValueError
        private, fork, archived = value["private"], value["fork"], value["archived"]
        if any(type(v) is not bool for v in (private, fork, archived)):
            raise ValueError
        visibility = "private" if private else "public"
        if value.get("visibility", visibility) != visibility:
            raise ValueError
        # Never trust an API-supplied clone target for secret delivery.
        url = f"https://github.com/{login}/{name}"
        if value["clone_url"] != url + ".git" or value["html_url"] != url:
            raise ValueError
        branch, created = value["default_branch"], value["created_at"]
        if branch is not None and (not isinstance(branch, str) or len(branch) > 255):
            raise ValueError
        if not isinstance(created, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", created
        ):
            raise ValueError
        return Repository(key, login, name, visibility, fork, archived, branch, created)
    except (KeyError, TypeError, ValueError, AttributeError, HarborError):
        raise DiscoveryError("GITHUB_INVALID_REPOSITORY") from None


class GitHub:
    def __init__(self, credentials: Credentials):
        self.credentials = credentials
        self.opener = build_opener(NoRedirect())
        self.deadline = time.monotonic() + 120

    def request(self, path: str, credential: str) -> tuple[Any, Mapping[str, str]]:
        if time.monotonic() >= self.deadline:
            raise DiscoveryError("GITHUB_DISCOVERY_TIMEOUT")
        req = Request(
            BASE + path,
            headers={
                "Authorization": "Bearer " + self.credentials.values[credential],
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "RepoHarbor/0.1.0",
            },
        )
        try:
            with self.opener.open(req, timeout=15) as response:
                body = bytearray()
                while True:
                    if time.monotonic() >= self.deadline:
                        raise DiscoveryError("GITHUB_DISCOVERY_TIMEOUT")
                    part = response.read(65536)
                    body.extend(part)
                    if len(body) > MAX_BODY:
                        raise DiscoveryError("GITHUB_RESPONSE_TOO_LARGE")
                    if not part:
                        break
                headers = response.headers
            data = json.loads(body)
            return data, headers
        except HTTPError as error:
            with error:
                rate = error.code == 429 or (
                    error.code == 403
                    and (
                        error.headers.get("X-RateLimit-Remaining") == "0"
                        or bool(error.headers.get("Retry-After"))
                        or b"rate limit" in error.read(4096).lower()
                    )
                )
                code = (
                    "GITHUB_RATE_LIMIT"
                    if rate
                    else {
                        401: "GITHUB_UNAUTHORIZED",
                        403: "GITHUB_FORBIDDEN",
                        404: "GITHUB_NOT_ACCESSIBLE",
                    }.get(error.code, "GITHUB_HTTP_ERROR")
                )
                retry = (
                    retry_time(dict(error.headers.items()), time.time()) if rate else 0
                )
            raise DiscoveryError(code, retry) from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise DiscoveryError("GITHUB_INVALID_RESPONSE_OR_NETWORK") from None

    def discover(self, provider: Provider) -> list[Observation]:
        user, _ = self.request("/user", provider.credential)
        if not isinstance(user, dict) or not isinstance(user.get("login"), str):
            raise DiscoveryError("GITHUB_INVALID_USER")
        found: dict[str, Observation] = {}
        path = (
            "/user/repos?per_page=100"
            "&affiliation=owner,collaborator,organization_member&page="
        )
        for page in range(1, 1001):
            values, headers = self.request(path + str(page), provider.credential)
            if not isinstance(values, list) or len(values) > 100:
                raise DiscoveryError("GITHUB_INVALID_PAGE")
            for value in values:
                repo = parse_repository(value)
                scope = (
                    provider.include_owned
                    if repo.owner.lower() == user["login"].lower()
                    else provider.include_organizations
                    if value["owner"]["type"] == "Organization"
                    else provider.include_collaborators
                )
                observation = Observation(repo, provider.credential, scope)
                if repo.key in found and found[repo.key] != observation:
                    raise DiscoveryError("GITHUB_INCONSISTENT_ID")
                found[repo.key] = observation
            next_page = self.next_page(headers.get("Link", ""), page)
            if not next_page and len(values) < 100:
                return list(found.values())
        raise DiscoveryError("GITHUB_PAGINATION_LIMIT")

    @staticmethod
    def next_page(link: str, page: int) -> bool:
        pattern = r'<([^>]+)>;\s*rel="(next|last|first|prev)"'
        links = re.findall(pattern, link)
        if re.sub(pattern, "", link).strip(" ,") or len(
            {rel for _, rel in links}
        ) != len(links):
            raise DiscoveryError("GITHUB_INVALID_PAGINATION")
        has_next = False
        for url, relation in links:
            try:
                parsed = urlsplit(url)
                query = parse_qs(parsed.query)
            except ValueError:
                raise DiscoveryError("GITHUB_INVALID_PAGINATION") from None
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.github.com"
                or parsed.path != "/user/repos"
                or parsed.fragment
                or query.get("per_page") != ["100"]
                or len(query.get("page", [])) != 1
                or not query["page"][0].isdigit()
                or set(query) - {"page", "per_page", "affiliation"}
                or (
                    "affiliation" in query
                    and query["affiliation"]
                    != ["owner,collaborator,organization_member"]
                )
                or (relation == "next" and query["page"] != [str(page + 1)])
            ):
                raise DiscoveryError("GITHUB_INVALID_PAGINATION")
            has_next = has_next or relation == "next"
        return has_next
