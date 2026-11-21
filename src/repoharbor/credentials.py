"""Resolve one explicit secret source; pass secrets only to a scoped askpass."""

import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from repoharbor.config import Config
from repoharbor.errors import HarborError

MAX_TOKEN_BYTES = 8192


def read_token(path: Path) -> str:
    directory = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDONLY
    with ExitStack() as stack:
        fd = os.open("/", directory)
        stack.callback(os.close, fd)
        for component in path.parts[1:-1]:
            fd = os.open(component, directory, dir_fd=fd)
            stack.callback(os.close, fd)
        with os.fdopen(os.open(path.name, flags, dir_fd=fd), "rb") as stream:
            entry = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(entry.st_mode)
                or entry.st_uid not in (0, os.geteuid())
                or entry.st_mode & 0o077
            ):
                raise HarborError(
                    "Credential file must be owner-readable and private (0400 or 0600)"
                )
            value = stream.read(MAX_TOKEN_BYTES + 1)
    if len(value) > MAX_TOKEN_BYTES:
        raise HarborError("Credential file exceeds the supported size")
    return value.decode("ascii").rstrip("\r\n")


@dataclass(repr=False)
class Credentials:
    values: dict[str, str] = field(repr=False)

    @classmethod
    def resolve(cls, config: Config, env: dict[str, str]) -> "Credentials":
        values: dict[str, str] = {}
        for name in config.credentials:
            key = "REPOHARBOR_TOKEN_" + name.upper().replace("-", "_")
            path = config.secrets_dir / name
            if config.token_source == "env":
                if path.exists():
                    raise HarborError("Mixed credential modes")
                value = env.get(key, "")
            else:
                if key in env:
                    raise HarborError("Mixed credential modes")
                try:
                    value = read_token(path)
                except (OSError, UnicodeError):
                    raise HarborError("Missing or unreadable credential file") from None
            if (
                not value
                or not value.isascii()
                or any(not 33 <= ord(c) < 127 for c in value)
            ):
                raise HarborError("Missing or malformed credential")
            values[name] = value
        return cls(values)

    def git_env(self, name: str | None) -> dict[str, str]:
        # Do not inherit arbitrary Git config, proxies, loaders or unrelated secrets.
        env = {
            k: os.environ[k]
            for k in ("PATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if k in os.environ
        }
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_LFS_SKIP_SMUDGE": "1",
                "GIT_ASKPASS": str(Path(__file__).with_name("askpass.py")),
                "GIT_CONFIG_COUNT": "10",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "GIT_CONFIG_KEY_1": "http.followRedirects",
                "GIT_CONFIG_VALUE_1": "false",
                "GIT_CONFIG_KEY_2": "protocol.allow",
                "GIT_CONFIG_VALUE_2": "never",
                "GIT_CONFIG_KEY_3": "protocol.https.allow",
                "GIT_CONFIG_VALUE_3": "always",
                "GIT_CONFIG_KEY_4": "core.hooksPath",
                "GIT_CONFIG_VALUE_4": "/dev/null",
                "GIT_CONFIG_KEY_5": "http.sslVerify",
                "GIT_CONFIG_VALUE_5": "true",
                "GIT_CONFIG_KEY_6": "core.fsmonitor",
                "GIT_CONFIG_VALUE_6": "false",
                "GIT_CONFIG_KEY_7": "fetch.writeCommitGraph",
                "GIT_CONFIG_VALUE_7": "false",
                "GIT_CONFIG_KEY_8": "core.fsync",
                "GIT_CONFIG_VALUE_8": "all",
                "GIT_CONFIG_KEY_9": "fetch.fsckObjects",
                "GIT_CONFIG_VALUE_9": "true",
            }
        )
        if "SSL_CERT_FILE" in env:
            # libcurl-backed Git needs its own CA setting for the same trusted bundle.
            env["GIT_SSL_CAINFO"] = env["SSL_CERT_FILE"]
        if name is not None:
            env["REPOHARBOR_ASKPASS_TOKEN"] = self.values[name]
        return env
