"""Resolve one explicit secret source; pass secrets only to a scoped askpass."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from repoharbor.config import Config
from repoharbor.errors import HarborError


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
                    if path.is_symlink() or not path.is_file():
                        raise OSError
                    value = path.read_text().rstrip("\r\n")
                except (OSError, UnicodeError):
                    raise HarborError("Missing or unreadable credential file") from None
            if not value or any(ord(c) < 32 for c in value):
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
                "GIT_CONFIG_COUNT": "8",
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
            }
        )
        if name is not None:
            env["REPOHARBOR_ASKPASS_TOKEN"] = self.values[name]
        return env
