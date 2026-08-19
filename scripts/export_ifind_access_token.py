"""Acquire one iFinD access token and share it across GitHub Action steps."""

from __future__ import annotations

import os
from pathlib import Path

from china_commodities.collectors.ifind_http_adapter import IFindHTTPClient


def main() -> int:
    github_environment = os.environ.get("GITHUB_ENV")
    if not github_environment:
        raise RuntimeError("GITHUB_ENV is required")
    token = IFindHTTPClient().get_access_token()
    print(f"::add-mask::{token}")
    with Path(github_environment).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"IFIND_ACCESS_TOKEN={token}\n")
    print("Shared iFinD access token is ready for this workflow run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
