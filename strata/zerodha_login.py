"""Daily Kite login: python -m strata.zerodha_login

1. Open the printed URL on your phone/laptop and log in to Zerodha.
2. You are redirected to your app's redirect URL; copy the `request_token=...` value from it.
3. Paste it here. The token is saved to state/zerodha_token.json for today only.
"""
import os
import sys
from datetime import datetime

from .config import load_settings
from .state import save_json


def main():
    s = load_settings("zerodha")
    from kiteconnect import KiteConnect
    key, secret = s.env.get("KITE_API_KEY"), s.env.get("KITE_API_SECRET")
    if not (key and secret):
        sys.exit("KITE_API_KEY / KITE_API_SECRET missing in .env")
    kite = KiteConnect(api_key=key)
    print("Log in here:\n ", kite.login_url())
    req = input("request_token: ").strip()
    data = kite.generate_session(req, api_secret=secret)
    path = s.state_dir / "zerodha_token.json"
    save_json(path, {"access_token": data["access_token"], "date": datetime.now().strftime("%Y-%m-%d")})
    os.chmod(path, 0o600)
    print("Saved. Strata picks it up on its next loop.")


if __name__ == "__main__":
    main()
