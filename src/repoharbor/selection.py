"""Provider-independent selection precedence and stable identity selectors."""

import re
from dataclasses import dataclass

from repoharbor.errors import HarborError


def instance_host(value: str) -> str:
    """Canonical DNS authority, optionally with an explicit non-default port."""
    if not isinstance(value, str) or len(value) > 259:
        raise HarborError("Invalid GitLab host")
    match = re.fullmatch(
        r"([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::([1-9][0-9]{0,4}))?", value
    )
    if not match:
        raise HarborError("Invalid GitLab host")
    host, port = match.groups()
    if (
        any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
            for x in host.split(".")
        )
        or "." not in host
    ):
        raise HarborError("Invalid GitLab host")
    if port and (int(port) > 65535 or port == "443"):
        raise HarborError("Invalid or noncanonical GitLab port")
    return value


def repository_key(value: str) -> str:
    if not isinstance(value, str):
        raise HarborError("Invalid provider repository identity")
    if re.fullmatch(r"github:github\.com:[1-9][0-9]{0,19}", value):
        return value
    match = re.fullmatch(r"gitlab:(.+):([1-9][0-9]{0,19})", value)
    if not match:
        raise HarborError("Invalid provider repository identity")
    instance_host(match[1])
    return value


def address(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise HarborError("Invalid repository selector")
    value = value.removeprefix("https://").rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    host, *parts = value.split("/")
    instance_host(host)
    if len(parts) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in (".", "..") for p in parts
    ):
        raise HarborError("Invalid repository selector")
    if host == "github.com" and len(parts) != 2:
        raise HarborError("Invalid repository selector")
    return value.lower()


def selector(value: str) -> str:
    return (
        repository_key(value)
        if isinstance(value, str) and value.startswith(("github:", "gitlab:"))
        else address(value)
    )


@dataclass(frozen=True)
class Selection:
    mode: str = "selected"
    visibility: tuple[str, ...] = ("public", "private")
    include_forks: bool = False
    include_archived: bool = False
    selected: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    def reason(
        self, key: str, names: set[str], visibility: str, fork: bool, archived: bool
    ) -> str | None:
        matches = names | {key}
        if matches.intersection(self.excluded):
            return "explicit_exclusion"
        if visibility not in self.visibility:
            return "visibility_filter"
        if fork and not self.include_forks:
            return "fork_filter"
        if archived and not self.include_archived:
            return "archived_filter"
        if self.mode == "selected" and not matches.intersection(self.selected):
            return "not_selected"
        return None
