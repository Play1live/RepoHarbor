"""Bounded GitHub REST reads; no redirects or remote diagnostic disclosure."""

from __future__ import annotations

import contextlib
import errno
import json
import math
import queue
import re
import socket
import sys
import threading
import time
from collections.abc import Mapping
from email.message import Message
from email.utils import parsedate_to_datetime
from http.client import HTTPException, IncompleteRead, RemoteDisconnected
from ssl import SSLError
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from repoharbor import __version__
from repoharbor.config import Provider
from repoharbor.credentials import Credentials
from repoharbor.errors import HarborError
from repoharbor.providers import Observation, Repository
from repoharbor.selection import address, repository_key

BASE = "https://api.github.com"
MAX_BODY = 4 * 1024 * 1024
TEMPORARY_HTTP_ERRORS = (500, 502, 503, 504)
NETWORK_WORKERS = threading.BoundedSemaphore(4)


class DiscoveryError(HarborError):
    def __init__(self, code: str, retry_at: float = 0, *, temporary: bool = False):
        super().__init__(code)
        self.retry_at = retry_at
        self.temporary = temporary


def temporary_transport_failure(error: Exception) -> bool:
    cause = error.reason if isinstance(error, URLError) else error
    if isinstance(cause, SSLError):
        return False
    if isinstance(cause, socket.gaierror):
        return cause.errno == socket.EAI_AGAIN
    return isinstance(cause, (TimeoutError, RemoteDisconnected, IncompleteRead)) or (
        isinstance(cause, OSError)
        and cause.errno
        in (
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.EHOSTUNREACH,
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
        )
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def normalized_headers(
    headers: Mapping[str, str] | Message[str, str],
) -> list[tuple[str, str]]:
    return [(name.lower(), value.strip()) for name, value in headers.items()]


def quota_exhausted(headers: Mapping[str, str] | Message[str, str]) -> bool:
    # Duplicate contradictory values are conservative: a valid zero wins.
    # Match digits without converting untrusted, potentially huge integers.
    return any(
        name == "x-ratelimit-remaining" and re.fullmatch(r"0+", value)
        for name, value in normalized_headers(headers)
    )


def retry_time(
    headers: Mapping[str, str] | Message[str, str],
    now: float,
    *,
    require_reset: bool = False,
) -> float:
    times = []
    valid_reset = False
    for name, value in normalized_headers(headers):
        if name not in {"retry-after", "x-ratelimit-reset"}:
            continue
        try:
            number = float(value)
        except ValueError:
            if name == "retry-after":
                with contextlib.suppress(ValueError, TypeError, OverflowError):
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is not None and date.timestamp() > now:
                        times.append(date.timestamp())
            continue
        if not math.isfinite(number) or number < 0:
            continue
        if name == "retry-after":
            # Saturate arithmetic overflow, never shorten a finite server delay.
            times.append(min(now + number, sys.float_info.max))
        elif number > now:
            times.append(number)
            valid_reset = True
    if require_reset and not valid_reset:
        times.append(now + 60)
    # A server cooldown is persisted, not slept here or capped to one day.
    return max(times, default=now + 60)


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
        visibility = value.get("visibility", "private" if private else "public")
        if visibility not in (
            ("private", "internal") if private else ("public", "internal")
        ):
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
    base = BASE
    quota_exhausted = staticmethod(quota_exhausted)
    retry_time = staticmethod(retry_time)

    def headers(self, credential: str) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.values[credential],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"RepoHarbor/{__version__}",
        }

    def __init__(
        self,
        credentials: Credentials,
        *,
        request_delay_ms: int = 1000,
        stop: threading.Event | None = None,
    ):
        self.credentials = credentials
        self.opener = build_opener(NoRedirect())
        self.deadline = time.monotonic() + 120
        self.stop = stop if stop is not None else threading.Event()
        self.delay = request_delay_ms / 1000
        self.last_completed: float | None = None
        self.retry_at = 0.0
        self.lock = threading.Lock()
        self.network = threading.local()

    def request(self, path: str, credential: str) -> tuple[Any, Mapping[str, str]]:
        # One client is shared by all providers, pages and identity checks per run.
        with self.lock:
            while True:
                if self.stop.is_set():
                    raise DiscoveryError("INTERRUPTED")
                now = time.monotonic()
                if now >= self.deadline:
                    raise DiscoveryError("GITHUB_DISCOVERY_TIMEOUT")
                if self.retry_at > time.time():
                    raise DiscoveryError("GITHUB_RATE_LIMIT", self.retry_at)
                remaining = (
                    self.last_completed + self.delay - now
                    if self.last_completed is not None
                    else 0
                )
                if remaining <= 0:
                    break
                self.stop.wait(min(remaining, self.deadline - now))
            try:
                return self.bounded_request(path, credential)
            except (HTTPException, OSError, ValueError, RecursionError) as error:
                raise DiscoveryError(
                    "GITHUB_INVALID_RESPONSE_OR_NETWORK",
                    self.retry_at,
                    temporary=temporary_transport_failure(error),
                ) from None
            finally:
                self.last_completed = time.monotonic()

    def bounded_request(
        self, path: str, credential: str
    ) -> tuple[Any, Mapping[str, str]]:
        # DNS and some blocking TLS reads cannot be cancelled inside urllib. Workers
        # never write persistent state, and a global cap bounds abandoned I/O.
        if not NETWORK_WORKERS.acquire(blocking=False):
            raise DiscoveryError("GITHUB_DISCOVERY_TIMEOUT", temporary=True)
        outcome: queue.Queue[tuple[Any, Mapping[str, str]] | Exception | None] = (
            queue.Queue()
        )
        acknowledged = threading.Event()
        cancelled = threading.Event()

        def cooldown() -> None:
            outcome.put(None)
            acknowledged.wait()
            if cancelled.is_set():
                raise DiscoveryError("GITHUB_DISCOVERY_TIMEOUT", temporary=True)

        def receive() -> None:
            self.network.cooldown = cooldown
            try:
                outcome.put(self._request(path, credential))
            except Exception as error:
                outcome.put(error)
            finally:
                NETWORK_WORKERS.release()

        thread = threading.Thread(
            target=receive, name="repoharbor-discovery", daemon=True
        )
        try:
            thread.start()
        except BaseException:
            NETWORK_WORKERS.release()
            raise
        try:
            while not self.stop.is_set():
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise DiscoveryError("GITHUB_DISCOVERY_TIMEOUT", temporary=True)
                try:
                    result = outcome.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                if result is None:
                    self.persist_cooldown()
                    acknowledged.set()
                elif isinstance(result, Exception):
                    raise result
                else:
                    return result
            raise DiscoveryError("INTERRUPTED")
        finally:
            cancelled.set()
            acknowledged.set()

    def report_cooldown(self) -> None:
        callback = getattr(self.network, "cooldown", self.persist_cooldown)
        callback()

    def persist_cooldown(self) -> None:
        pass

    def _request(self, path: str, credential: str) -> tuple[Any, Mapping[str, str]]:
        req = Request(self.base + path, headers=self.headers(credential))
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise DiscoveryError("GITHUB_DISCOVERY_TIMEOUT")
            with self.opener.open(req, timeout=min(15, remaining)) as response:
                status = getattr(response, "status", 200)
                if type(status) is int and status != 200:
                    raise DiscoveryError(
                        "GITHUB_HTTP_ERROR", temporary=status in TEMPORARY_HTTP_ERRORS
                    )
                headers = response.headers
                if self.quota_exhausted(headers):
                    self.retry_at = max(
                        self.retry_at,
                        self.retry_time(headers, time.time(), require_reset=True),
                    )
                    self.report_cooldown()
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
            data = json.loads(body)
            return data, headers
        except HTTPError as error:
            with error:
                headers = dict(normalized_headers(error.headers))
                rate = error.code == 429 or (
                    error.code == 403
                    and (
                        self.quota_exhausted(error.headers)
                        or bool(headers.get("retry-after"))
                        or self.rate_limit_message(error)
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
                if rate:
                    self.retry_at = max(
                        self.retry_at, self.retry_time(error.headers, time.time())
                    )
                    self.report_cooldown()
                retry = self.retry_at if rate else 0
            raise DiscoveryError(
                code, retry, temporary=error.code in TEMPORARY_HTTP_ERRORS
            ) from None

    def rate_limit_message(self, error: HTTPError) -> bool:
        try:
            return b"rate limit" in error.read(4096).lower()
        except (HTTPException, OSError, ValueError):
            # A failed read of a 403 body cannot make the response retryable.
            raise DiscoveryError(
                "GITHUB_INVALID_RESPONSE_OR_NETWORK", self.retry_at
            ) from None

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
