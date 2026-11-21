#!/usr/bin/env python3
"""Git-only child helper. Never log the prompt or token."""

import os
import re
import sys
from urllib.parse import urlsplit


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) == 2 else ""
    match = re.fullmatch(r"(Username|Password) for '([^']+)': ?", prompt)
    expected = os.environ.get("REPOHARBOR_ASKPASS_AUTHORITY")
    if match is None or not expected:
        sys.exit(1)
    try:
        url = urlsplit(match[2])
        # Git may add the previously supplied username to its password prompt.
        authority = url.netloc.rsplit("@", 1)[-1]
        if url.scheme != "https" or authority != expected or url.password is not None:
            sys.exit(1)
    except ValueError:
        sys.exit(1)
    print(
        "oauth2"
        if match[1] == "Username"
        else os.environ.get("REPOHARBOR_ASKPASS_TOKEN", "")
    )


if __name__ == "__main__":
    main()
