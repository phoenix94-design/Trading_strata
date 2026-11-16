"""Simulated broker: real prices from the platform, pretend orders, state saved to disk."""
import time
from dataclasses import asdict
from pathlib import Path

from ..models import Position
from ..state import load_json, save_json
from .base import Broker, MarketData

FEE_PCT = 0.1   # rough per-side cost (brokerage + taxes / exchange fee)


class PaperBroker(Broker):
    def __init__(self, platform, data: MarketData, state_dir: Path, start_cash: float):
        self.data = data
        self.path = Path(state_dir) / f"paper_{platform}.json"
        s = load_json(self.path, {"cash": start_cash, "positions": {}})
        self._cash = s["cash"]
        self._pos = {k: Position(**v) for k, v in s["positions"].items()}

    def _save(self):
        save_json(self.path, {"cash": self._cash, "positions": {k: asdict(v) for k, v in self._pos.items()}})

    def cash(self):
        return self._cash

    def positions(self):
        return dict(self._pos)

    def buy(self, symbol, qty, stop):
        price = self.data.last_price(symbol)
        cost = price * qty * (1 + FEE_PCT / 100)
        if cost > self._cash:
            raise RuntimeError(f"paper: not enough cash for {qty} {symbol}")
        self._cash -= cost
        pos = Position(symbol, qty, price, stop, int(time.time()), stop_order_id="paper")
        self._pos[symbol] = pos
        self._save()
        return pos

    def _close(self, symbol, price):
        pos = self._pos.pop(symbol)
        self._cash += price * pos.qty * (1 - FEE_PCT / 100)
        self._save()
        return price

    def sell(self, symbol, reason):
        return self._close(symbol, self.data.last_price(symbol))

    def move_stop(self, symbol, new_stop):
        self._pos[symbol].stop = new_stop
        self._save()

    def check_stops(self):
        hit = []
        for sym, pos in list(self._pos.items()):
            price = self.data.last_price(sym)
            if price <= pos.stop:
                hit.append((sym, self._close(sym, price)))
        return hit
