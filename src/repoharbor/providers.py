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
    def host(self) -> str:
        return self.key.split(":", 1)[1].rsplit(":", 1)[0]

    @property
    def provider(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def address(self) -> str:
        return f"{self.host}/{self.owner}/{self.name}".lower()

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}.git"


@dataclass(frozen=True)
class Observation:
    repository: Repository
    credential: str
    in_scope: bool
