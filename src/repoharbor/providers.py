"""Typed observations shared by discovery, selection and the registry."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Repository:
    key: str
    owner: str
    name: str
    visibility: str
    fork: bool
    archived: bool
    default_branch: str | None
    created_at: str

    @property
    def address(self) -> str:
        return f"github.com/{self.owner}/{self.name}".lower()

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"


@dataclass(frozen=True)
class Observation:
    repository: Repository
    credential: str
    in_scope: bool
