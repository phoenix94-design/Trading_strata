"""CoinDCX spot (INR or USDT markets).

Public market data needs no key. Live orders are signed with HMAC-SHA256.
CoinDCX spot has no stop order Strata relies on, so in live mode the stop is
watched by Strata itself every loop (crypto trades 24/7, so the loop never sleeps).
"""
import hashlib
import hmac
import json
import math
import time
from dataclasses import asdict
from pathlib import Path

import requests

from ..models import Candle, Position
from ..state import load_json, save_json
from .base import Broker, MarketData

API = "https://api.coindcx.com"
PUBLIC = "https://public.coindcx.com"
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
                    "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "1d": 86400}


class CoinDCX(MarketData, Broker):
    def __init__(self, settings):
        self.key = settings.env.get("COINDCX_API_KEY", "")
        self.secret = settings.env.get("COINDCX_API_SECRET", "")
        self.http = requests.Session()
        self._markets = None
        self.quote = settings.cfg.get("quote_currency", "INR")
        self.path = Path(settings.state_dir) / "live_coindcx.json"
        self._pos = {k: Position(**v) for k, v in load_json(self.path, {}).items()}

    # ---------- market data ----------
    def _get(self, url, **params):
        r = self.http.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def markets(self):
        if self._markets is None:
            self._markets = {m["pair"]: m for m in self._get(f"{API}/exchange/v1/markets_details")}
        return self._markets

    def candles(self, symbol, interval, limit):
        raw = self._get(f"{PUBLIC}/market_data/candles", pair=symbol, interval=interval, limit=min(limit + 1, 1000))
        now = time.time()
        step = INTERVAL_SECONDS[interval]
        out = [Candle(int(c["time"]) // 1000, float(c["open"]), float(c["high"]), float(c["low"]),
                      float(c["close"]), float(c.get("volume", 0))) for c in raw]
        out.sort(key=lambda c: c.ts)
        return [c for c in out if c.ts + step <= now][-limit:]   # drop the still-forming candle

    def last_price(self, symbol):
        market = self.markets()[symbol]["coindcx_name"]
        for t in self._get(f"{API}/exchange/ticker"):
            if t["market"] == market:
                return float(t["last_price"])
        raise RuntimeError(f"no ticker for {symbol}")

    def lot(self, symbol):
        m = self.markets()[symbol]
        step = float(m.get("step") or 10 ** -int(m.get("target_currency_precision", 8)))
        return step, float(m.get("min_quantity", step))

    # ---------- account (live) ----------
    def _signed(self, path, body):
        if not (self.key and self.secret):
            raise RuntimeError("COINDCX_API_KEY / COINDCX_API_SECRET missing in .env")
        body = {**body, "timestamp": int(time.time() * 1000)}
        payload = json.dumps(body, separators=(",", ":"))
        sig = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        r = self.http.post(f"{API}{path}", data=payload, timeout=15, headers={
            "Content-Type": "application/json", "X-AUTH-APIKEY": self.key, "X-AUTH-SIGNATURE": sig})
        if r.status_code >= 400:
            raise RuntimeError(f"CoinDCX {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _balance(self, currency):
        for b in self._signed("/exchange/v1/users/balances", {}):
            if b["currency"] == currency:
                return float(b["balance"])
        return 0.0

    def _save(self):
        save_json(self.path, {k: asdict(v) for k, v in self._pos.items()})

    def cash(self):
        return self._balance(self.quote)

    def positions(self):
        return dict(self._pos)

    def _order(self, symbol, side, qty):
        m = self.markets()[symbol]
        return self._signed("/exchange/v1/orders/create", {
            "side": side, "order_type": "market_order", "market": m["coindcx_name"], "total_quantity": qty})

    def buy(self, symbol, qty, stop):
        price = self.last_price(symbol)
        self._order(symbol, "buy", qty)
        self._pos[symbol] = Position(symbol, qty, price, stop, int(time.time()), stop_order_id=None)
        self._save()
        return self._pos[symbol]

    def sell(self, symbol, reason):
        pos = self._pos[symbol]
        step, _ = self.lot(symbol)
        held = self._balance(self.markets()[symbol]["target_currency_short_name"])
        qty = math.floor(min(pos.qty, held) / step) * step
        price = self.last_price(symbol)
        if qty > 0:
            self._order(symbol, "sell", round(qty, 10))
        del self._pos[symbol]
        self._save()
        return price

    def move_stop(self, symbol, new_stop):
        self._pos[symbol].stop = new_stop
        self._save()

    def check_stops(self):
        hit = []
        for sym, pos in list(self._pos.items()):
            if self.last_price(sym) <= pos.stop:
                hit.append((sym, self.sell(sym, "stop")))
        return hit

    def sync(self):
        gone = []
        for sym, pos in list(self._pos.items()):
            if self._balance(self.markets()[sym]["target_currency_short_name"]) < pos.qty * 0.5:
                del self._pos[sym]
                gone.append((sym, self.last_price(sym)))
        if gone:
            self._save()
        return gone
