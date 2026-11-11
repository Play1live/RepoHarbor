#!/usr/bin/env python3
"""Git-only child helper. Never log the prompt or token."""

import os
import sys


def main() -> None:
    if "username" in sys.argv[1].lower():
        print("oauth2")
    else:
        print(os.environ.get("REPOHARBOR_ASKPASS_TOKEN", ""))


if __name__ == "__main__":
    main()
