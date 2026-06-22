#!/usr/bin/env python3
"""Live API smoke test — SafeHaven, Paystack, X. Run with real .env credentials."""

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    ok = True

    print("=== X API ===")
    try:
        from app.x_client import x_credentials_configured, get_x_client

        if not x_credentials_configured():
            print("SKIP: X credentials not set")
        else:
            client = get_x_client()
            identity = client.get_bot_identity()
            print(f"OK: @{identity['username']} ({identity['user_id']})")
    except Exception as exc:
        print(f"FAIL: {exc}")
        ok = False

    print("\n=== SafeHaven ===")
    try:
        from app.payments.safehaven import SafeHavenClient

        sh = SafeHavenClient()
        if sh.mock:
            print("SKIP: SAFEHAVEN_MOCK=true")
        elif not sh.configured():
            print("SKIP: SafeHaven not configured")
        else:
            token = sh._ensure_token()
            banks = sh.list_banks()
            print(f"OK: token acquired, {len(banks)} banks listed")
            assert token
    except Exception as exc:
        print(f"FAIL: {exc}")
        ok = False

    print("\n=== Paystack ===")
    try:
        from app.payments.paystack import PaystackClient

        ps = PaystackClient()
        if ps.mock:
            print("SKIP: PAYSTACK_MOCK=true")
        elif not ps.configured():
            print("SKIP: Paystack not configured")
        else:
            import requests

            resp = requests.get(
                "https://api.paystack.co/bank",
                headers=ps._headers(),
                params={"country": "nigeria"},
                timeout=30,
            )
            data = resp.json()
            if not data.get("status"):
                raise RuntimeError(data.get("message"))
            print(f"OK: Paystack reachable, {len(data.get('data', []))} banks")
    except Exception as exc:
        print(f"FAIL: {exc}")
        ok = False

    print("\n=== Result ===")
    if ok:
        print("All checks passed (or skipped).")
        return 0
    print("Some checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())