"""One-time-per-day interactive helper: exchanges a Kite Connect request_token for an
access_token. Run with: python -m scripts.kite_login
Requires KITE_API_KEY and KITE_API_SECRET in the environment (or a .env you've sourced).
"""

import hashlib
import os

import requests

API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")


def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET first.")

    login_url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
    print(f"1. Open this URL, log in, and copy the `request_token` from the redirect URL:\n   {login_url}\n")
    request_token = input("2. Paste request_token here: ").strip()

    checksum = hashlib.sha256(f"{API_KEY}{request_token}{API_SECRET}".encode()).hexdigest()
    resp = requests.post(
        "https://api.kite.trade/session/token",
        data={"api_key": API_KEY, "request_token": request_token, "checksum": checksum},
        headers={"X-Kite-Version": "3"},
        timeout=15,
    )
    resp.raise_for_status()
    access_token = resp.json()["data"]["access_token"]
    print(f"\nKITE_ACCESS_TOKEN={access_token}\n\nAdd that line to .env — it's valid until ~6am IST tomorrow.")


if __name__ == "__main__":
    main()
