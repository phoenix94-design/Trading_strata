"""CoinDCX futures REST client.

Checked against docs.coindcx.com (quoted from the home server, 2026-11-17). Facts the code relies on:

* Pairs look like B-BTC_USDT. The same pairs trade with USDT or INR margin
  (margin_currency_short_name). The app's "Global Futures" (S&P500, NVDA, ...) are NOT in this API.
* Create order: body {"timestamp", "order": {...}}. Market orders: no price, no time_in_force.
  There is NO reduce-only flag, so a sell on a long only reduces it while the long exists.
* create_tpsl puts ONE take-profit and ONE stop-loss on the WHOLE position (take_profit_market /
  stop_market only). They sit on CoinDCX, so they work while the home server is offline.
* positions / orders / cancel-all default to USDT margin: INR-margin calls must pass
  margin_currency_short_name=["INR"].
* /data/trades returns the last ~30 trades, oldest first.
* Signed calls: HMAC-SHA256 of the compact JSON body; timestamps older than 10 s are rejected.
"""
import hashlib
import hmac
import json
import time

import requests

HOST = "https://api.coindcx.com"
API = f"{HOST}/exchange/v1/derivatives/futures"


class CoinDCXError(RuntimeError):
    pass


class CoinDCXFutures:
    def __init__(self, api_key="", api_secret="", margin_currency="USDT", notification="email_notification"):
        self.key, self.secret = api_key, api_secret
        self.margin_currency = margin_currency
        self.notification = notification
        self.http = requests.Session()

    # ================= public =================
    def _get(self, url, **params):
        r = self.http.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def trades(self, pair):
        """Recent trades, oldest first: [{"price", "quantity", "timestamp" (ms), "is_maker"}]."""
        return self._get(f"{API}/data/trades", pair=pair)

    def last_price(self, pair):
        trades = self.trades(pair)
        return float(max(trades, key=lambda t: t["timestamp"])["price"])

    def instrument(self, pair, margin_currency=None):
        params = {"pair": pair}
        if margin_currency:
            params["margin_currency_short_name"] = margin_currency
        data = self._get(f"{API}/data/instrument", **params)
        return data.get("instrument", data)

    def spot_inr_per_usdt(self):
        """Spot USDT/INR price. Only an approximation of the futures conversion rate (paper mode)."""
        for t in self._get(f"{HOST}/exchange/ticker"):
            if t.get("market") == "USDTINR":
                return float(t["last_price"])
        raise CoinDCXError("USDTINR not found in spot ticker")

    # ================= signed =================
    def _post(self, path, body=None, base=API):
        if not (self.key and self.secret):
            raise CoinDCXError("COINDCX_API_KEY / COINDCX_API_SECRET missing in .env")
        body = dict(body or {})
        body["timestamp"] = int(round(time.time() * 1000))
        payload = json.dumps(body, separators=(",", ":"))           # the exact bytes that get signed
        sig = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        r = self.http.post(f"{base}{path}", data=payload, timeout=15, headers={
            "Content-Type": "application/json", "X-AUTH-APIKEY": self.key, "X-AUTH-SIGNATURE": sig})
        if r.status_code >= 400:
            raise CoinDCXError(f"CoinDCX {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}

    def conversion_inr_per_usdt(self):
        """The fixed rate CoinDCX uses to convert INR margin to USDT (and back)."""
        for row in self._post("/data/conversions", base=f"{HOST}/api/v1/derivatives/futures"):
            if row.get("symbol") == "USDTINR" or row.get("margin_currency_short_name") == "INR":
                return float(row["conversion_price"])
        raise CoinDCXError("no USDTINR conversion rate returned")

    # ---------- orders ----------
    def create_order(self, pair, side, order_type, qty, leverage, margin_currency=None, price=None,
                     position_margin_type="isolated"):
        """side "buy"/"sell"; order_type "market_order" or "limit_order". Returns the order dict."""
        order = {
            "side": side,
            "pair": pair,
            "order_type": order_type,
            "total_quantity": qty,
            "leverage": int(leverage),
            "notification": self.notification,
            "margin_currency_short_name": margin_currency or self.margin_currency,
            "position_margin_type": position_margin_type,
        }
        if order_type != "market_order":          # docs: no price / time_in_force on market orders
            order["price"] = str(price)
            order["time_in_force"] = "good_till_cancel"
        res = self._post("/orders/create", {"order": order})
        return res[0] if isinstance(res, list) and res else res

    def list_orders(self, side, statuses="open,partially_filled", margin_currency=None, size=50):
        """side is mandatory in the API. Filled qty = total - remaining - cancelled."""
        res = self._post("/orders", {"status": statuses, "side": side, "page": "1", "size": str(size),
                                     "margin_currency_short_name": [margin_currency or self.margin_currency]})
        return res if isinstance(res, list) else res.get("data", [])

    def cancel_order(self, order_id):
        return self._post("/orders/cancel", {"id": order_id})

    # ---------- positions ----------
    def positions(self, margin_currency=None, pairs=None):
        body = {"page": "1", "size": "100", "margin_currency_short_name": [margin_currency or self.margin_currency]}
        if pairs:
            body["pairs"] = pairs if isinstance(pairs, str) else ",".join(pairs)
        res = self._post("/positions", body)
        return res if isinstance(res, list) else res.get("data", [])

    def position_record(self, pair, margin_currency=None):
        """The position row for a pair (even if flat: it carries the id and the saved leverage).
        Fields may be null or 0 on a pair that was never traded."""
        for p in self.positions(margin_currency, pairs=pair):
            if p.get("pair") == pair:
                return p
        return None

    def open_position(self, pair, margin_currency=None):
        """The position for a pair if it is not flat. active_pos > 0 long, < 0 short."""
        p = self.position_record(pair, margin_currency)
        return p if p and float(p.get("active_pos") or 0) != 0 else None

    def update_leverage(self, pair, leverage, margin_currency=None):
        """Set the pair's leverage before ordering: an order whose leverage differs from the
        position's saved leverage is rejected (422). margin_currency_short_name is a plain string here."""
        return self._post("/positions/update_leverage", {
            "leverage": str(int(leverage)), "pair": pair,
            "margin_currency_short_name": margin_currency or self.margin_currency})

    def set_margin_type(self, pair, margin_type="isolated"):
        """isolated or crossed; only allowed with no position and no open orders on the pair.
        (Cross margin exists only for USDT margin; INR margin is always isolated.)"""
        return self._post("/positions/margin_type", {"pair": pair, "margin_type": margin_type})

    def create_tpsl(self, position_id, take_profit=None, stop_loss=None):
        """Whole-position take profit and/or stop loss (prices already formatted as strings).
        Raises if CoinDCX reports a failure, except "already exists"."""
        body = {"id": position_id}
        if take_profit is not None:
            body["take_profit"] = {"stop_price": str(take_profit), "order_type": "take_profit_market"}
        if stop_loss is not None:
            body["stop_loss"] = {"stop_price": str(stop_loss), "order_type": "stop_market"}
        res = self._post("/positions/create_tpsl", body)
        for part in ("take_profit", "stop_loss"):
            r = (res or {}).get(part)
            if isinstance(r, dict) and r.get("success") is False and "already exists" not in str(r.get("error", "")).lower():
                raise CoinDCXError(f"create_tpsl {part} failed: {r.get('error')}")
        return res

    def exit_position(self, position_id):
        """Close the whole position at market."""
        return self._post("/positions/exit", {"id": position_id})

    def cancel_position_orders(self, position_id):
        return self._post("/positions/cancel_all_open_orders_for_position", {"id": position_id})

    # ---------- used by the simple price-level bot (price_levels.py) ----------
    def market_order(self, pair, side, qty, leverage):
        return self.create_order(pair, side, "market_order", qty, leverage)

    def set_tpsl(self, position_id, take_profit, stop_loss, price_fmt):
        return self.create_tpsl(position_id, take_profit=price_fmt(take_profit), stop_loss=price_fmt(stop_loss))
