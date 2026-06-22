#!/usr/bin/env python3
"""Verify X API credentials before running a live giveaway."""

import sys
from pathlib import Path

# Allow running from repo root without installing as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.x_client import get_x_client, missing_credential_keys, x_credentials_configured


def main() -> int:
    if not x_credentials_configured():
        missing = ", ".join(missing_credential_keys())
        print(f"FAIL: missing credentials: {missing}")
        print("Fill them in .env (see .env.example), then re-run.")
        return 1

    try:
        identity = get_x_client().get_bot_identity()
    except Exception as exc:
        print(f"FAIL: could not authenticate with X API: {exc}")
        return 1

    print(f"OK: authenticated as @{identity['username']} (user_id={identity['user_id']})")
    print("Next: curl http://localhost:8000/health/x  (with the app running)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())