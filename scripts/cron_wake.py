#!/usr/bin/env python3
"""Render cron job: ping /internal/wake to keep the web service alive."""

import os
import sys

import requests


def main() -> int:
    host = os.getenv("WAKE_HOST", "").strip()
    if not host:
        print("FAIL: WAKE_HOST not set (Render fromService host property)")
        return 1

    url = f"https://{host}/internal/wake"
    headers = {}
    secret = os.getenv("CRON_WAKE_SECRET", "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    try:
        resp = requests.get(url, headers=headers, timeout=90)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"FAIL: wake request failed: {exc}")
        return 1

    print(f"OK: {resp.json()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())