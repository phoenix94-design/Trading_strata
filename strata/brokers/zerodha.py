"""Zerodha Kite Connect: NSE cash-market delivery (CNC), long only.

* Access tokens expire every morning (~6 AM). Run `venv/bin/python -m strata.zerodha_login`
  once a trading day; Strata waits (and pings nothing) until a valid token exists.
* After every buy Strata places a GTT stop-loss at Zerodha. A GTT lives on Zerodha's
  servers for up to a year, so the stop still works if the home server or internet is down.
* Live orders need the static IP registered with Kite (plan Phase 5, step 5).
"""
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..models import Candle, Position
from ..state import load_json, save_json
from .base import Broker, MarketData

INTERVALS = {"5m": "5minute", "15m": "15minute", "30m": "30minute", "1h": "60minute", "1d": "day"}
INTERVAL_DAYS = {"5m": 1 / 75, "15m": 1 / 25, "30m": 1 / 12, "1h": 1 / 6, "1d": 1.5}   # calendar days per candle, roughly
STOP_LIMIT_BUFFER = 0.01   # GTT sells with a limit 1% under the trigger so it fills in a fast drop


class Zerodha(MarketData, Broker):
    def __init__(self, settings, state_dir):
        from kiteconnect import KiteConnect
        self.kc_cls = KiteConnect
        self.api_key = settings.env.get("KITE_API_KEY", "")
        self.exchange = settings.cfg.get("exchange", "NSE")
        self.token_path = Path(state_dir) / "zerodha_token.json"
        self.pos_path = Path(state_dir) / "live_zerodha.json"
        self.kite = None
        self._tokens = None

    # ---------- session ----------
    def ready(self):
        """True when today's access token is present (Kite tokens die each morning)."""
        tok = load_json(self.token_path, {})
        if tok.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return False
        if self.kite is None or self.kite.access_token != tok["access_token"]:
            self.kite = self.kc_cls(api_key=self.api_key)
            self.kite.set_access_token(tok["access_token"])
        return True

    def _k(self):
        if not self.ready():
            raise RuntimeError("no Kite access token for today: run `venv/bin/python -m strata.zerodha_login`")
        return self.kite

    def _token(self, symbol):
        if self._tokens is None:
            self._tokens = {i["tradingsymbol"]: i["instrument_token"] for i in self._k().instruments(self.exchange)}
        return self._tokens[symbol]

    # ---------- market data ----------
    def candles(self, symbol, interval, limit):
        days = max(5, int(limit * INTERVAL_DAYS[interval] * 1.6) + 5)
        to = datetime.now()
        rows = self._k().historical_data(self._token(symbol), to - timedelta(days=days), to, INTERVALS[interval])
        out = [Candle(int(r["date"].timestamp()), r["open"], r["high"], r["low"], r["close"], r.get("volume", 0))
               for r in rows]
        if out and interval != "1d" and to.timestamp() < out[-1].ts + _seconds(interval):
            out = out[:-1]                       # still forming
        if out and interval == "1d" and datetime.fromtimestamp(out[-1].ts).date() == to.date() \
                and (to.hour, to.minute) < (15, 30):
            out = out[:-1]
        return out[-limit:]

    def last_price(self, symbol):
        key = f"{self.exchange}:{symbol}"
        return float(self._k().ltp([key])[key]["last_price"])

    # ---------- account ----------
    def cash(self):
        return float(self._k().margins("equity")["available"]["live_balance"])

    def _saved(self):
        return {k: Position(**v) for k, v in load_json(self.pos_path, {}).items()}

    def _save(self, pos):
        from dataclasses import asdict
        save_json(self.pos_path, {k: asdict(v) for k, v in pos.items()})

    def positions(self):
        return self._saved()

    def _place_gtt(self, symbol, qty, stop):
        k = self._k()
        res = k.place_gtt(
            trigger_type=k.GTT_TYPE_SINGLE, tradingsymbol=symbol, exchange=self.exchange,
            trigger_values=[round(stop, 1)], last_price=self.last_price(symbol),
            orders=[{"transaction_type": k.TRANSACTION_TYPE_SELL, "quantity": int(qty),
                     "order_type": k.ORDER_TYPE_LIMIT, "product": k.PRODUCT_CNC,
                     "price": round(stop * (1 - STOP_LIMIT_BUFFER), 1)}])
        return str(res["trigger_id"])

    def buy(self, symbol, qty, stop):
        k = self._k()
        k.place_order(variety=k.VARIETY_REGULAR, exchange=self.exchange, tradingsymbol=symbol,
                      transaction_type=k.TRANSACTION_TYPE_BUY, quantity=int(qty),
                      product=k.PRODUCT_CNC, order_type=k.ORDER_TYPE_MARKET)
        time.sleep(2)
        price = self.last_price(symbol)
        pos = Position(symbol, int(qty), price, stop, int(time.time()))
        pos.stop_order_id = self._place_gtt(symbol, qty, stop)
        all_pos = self._saved()
        all_pos[symbol] = pos
        self._save(all_pos)
        return pos

    def sell(self, symbol, reason):
        k = self._k()
        all_pos = self._saved()
        pos = all_pos.pop(symbol)
        if pos.stop_order_id:
            try:
                k.delete_gtt(int(pos.stop_order_id))
            except Exception:
                pass                               # already triggered or gone
        k.place_order(variety=k.VARIETY_REGULAR, exchange=self.exchange, tradingsymbol=symbol,
                      transaction_type=k.TRANSACTION_TYPE_SELL, quantity=int(pos.qty),
                      product=k.PRODUCT_CNC, order_type=k.ORDER_TYPE_MARKET)
        self._save(all_pos)
        return self.last_price(symbol)

    def move_stop(self, symbol, new_stop):
        k = self._k()
        all_pos = self._saved()
        pos = all_pos[symbol]
        if pos.stop_order_id:
            try:
                k.delete_gtt(int(pos.stop_order_id))
            except Exception:
                pass
        pos.stop = new_stop
        pos.stop_order_id = self._place_gtt(symbol, pos.qty, new_stop)
        self._save(all_pos)

    def sync(self):
        """Drop positions whose GTT fired (or that were sold by hand) while we weren't looking."""
        held = {h["tradingsymbol"]: h["quantity"] + h.get("t1_quantity", 0) for h in self._k().holdings()}
        all_pos, gone = self._saved(), []
        for sym in list(all_pos):
            if held.get(sym, 0) < all_pos[sym].qty:
                all_pos.pop(sym)
                gone.append((sym, self.last_price(sym)))
        if gone:
            self._save(all_pos)
        return gone


def _seconds(interval):
    return {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}[interval]
