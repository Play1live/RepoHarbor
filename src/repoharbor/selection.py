"""Provider-independent selection precedence and stable identity selectors."""

import re
from dataclasses import dataclass

from repoharbor.errors import HarborError


def repository_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"github:github\.com:[1-9][0-9]{0,19}", value
    ):
        raise HarborError("Invalid provider repository identity")
    return value


def address(value: str) -> str:
    if not isinstance(value, str):
        raise HarborError("Invalid repository selector")
    value = value.removeprefix("https://").rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", value):
        raise HarborError("Invalid repository selector")
    if value.split("/")[-1] in (".", ".."):
        raise HarborError("Invalid repository selector")
    return value.lower()


def selector(value: str) -> str:
    return (
        repository_key(value)
        if isinstance(value, str) and ":" in value and not value.startswith("https://")
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
