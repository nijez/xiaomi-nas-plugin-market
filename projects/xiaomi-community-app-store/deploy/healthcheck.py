#!/usr/bin/env python3
"""Verify that the community-store HTTP service is ready."""

from __future__ import annotations

import argparse
import time
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_URL = "http://127.0.0.1:18119/healthz"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    if not 0 < args.timeout <= 300:
        parser.error("--timeout must be within (0, 300] seconds")
    deadline = time.monotonic() + args.timeout
    error = "not ready"
    while time.monotonic() < deadline:
        try:
            with urlopen(args.url, timeout=min(2.0, max(0.01, deadline - time.monotonic()))) as response:
                if response.status == 200:
                    return 0
                error = f"HTTP {response.status}"
        except (OSError, URLError) as failure:
            error = str(failure)
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise SystemExit(f"Service not ready after {args.timeout:g}s: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
