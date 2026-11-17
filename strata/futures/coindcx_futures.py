"""Minimal CoinDCX futures REST client (only what the price-level strategy needs).

Endpoints come from CoinDCX's futures API as used by the open-source client
github.com/anujsainicse/coindcx-futures. docs.coindcx.com could not be reached when this
was written, so anything marked UNVERIFIED should be confirmed on the first small live test.
"""
import hashlib
import hmac
import json
import time

import requests

API = "https://api.coindcx.com/exchange/v1/derivatives/futures"


class CoinDCXFutures:
    def __init__(self, api_key="", api_secret="", margin_currency="USDT"):
        self.key, self.secret = api_key, api_secret
        self.margin_currency = margin_currency
        self.http = requests.Session()

    # ---------- public ----------
    def _get(self, path, **params):
        r = self.http.get(f"{API}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def last_price(self, pair):
        """Latest traded price, e.g. pair="B-BTC_USDT"."""
        trades = self._get("/data/trades", pair=pair)
        return float(trades[0]["price"])

    def instrument(self, pair):
        data = self._get("/data/instrument", pair=pair)
        return data.get("instrument", data)

    # ---------- signed ----------
    def _post(self, path, body=None):
        if not (self.key and self.secret):
            raise RuntimeError("COINDCX_API_KEY / COINDCX_API_SECRET missing in .env")
        body = dict(body or {})
        body["timestamp"] = int(round(time.time() * 1000))
        payload = json.dumps(body, separators=(",", ":"))           # the exact bytes that get signed
        sig = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        r = self.http.post(f"{API}{path}", data=payload, timeout=15, headers={
            "Content-Type": "application/json", "X-AUTH-APIKEY": self.key, "X-AUTH-SIGNATURE": sig})
        if r.status_code >= 400:
            raise RuntimeError(f"CoinDCX {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}

    def market_order(self, pair, side, qty, leverage):
        """Open (or add to) a position at market. side = "buy" (long) or "sell" (short)."""
        return self._post("/orders/create", {"order": {
            "side": side,
            "pair": pair,
            "order_type": "market_order",
            "total_quantity": qty,
            "leverage": int(leverage),
            "notification": "email_notification",
            "time_in_force": "good_till_cancel",
            "margin_currency_short_name": self.margin_currency,
            "position_margin_type": "isolated",
        }})

    def positions(self):
        res = self._post("/positions", {"page": "1", "size": "50"})
        return res if isinstance(res, list) else res.get("data", [])

    def open_position(self, pair):
        """The open position for a pair, or None. active_pos > 0 long, < 0 short."""
        for p in self.positions():
            if p.get("pair") == pair and float(p.get("active_pos") or 0) != 0:
                return p
        return None

    def set_tpsl(self, position_id, take_profit, stop_loss, price_fmt):
        """Attach take-profit and stop-loss to a position. They are held on CoinDCX's servers,
        so they still fire if the home server or internet is down."""
        return self._post("/positions/create_tpsl", {
            "id": position_id,
            "take_profit": {"stop_price": price_fmt(take_profit), "order_type": "take_profit_market"},
            "stop_loss": {"stop_price": price_fmt(stop_loss), "order_type": "stop_market"},
        })

    def exit_position(self, position_id):
        """Close the whole position at market."""
        return self._post("/positions/exit", {"id": position_id})

    def cancel_position_orders(self, position_id):
        return self._post("/positions/cancel_all_open_orders_for_position", {"id": position_id})
