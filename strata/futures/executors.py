"""Executors carry out what the trade manager decides.

PaperExecutor  simulated fills on real CoinDCX prices (TRADING_MODE=paper, the default)
LiveExecutor   real CoinDCX orders (TRADING_MODE=live)

The manager (trader.py) never talks to CoinDCX directly, so the strategy logic is identical in both
modes and can be tested with a fake price feed.

How a live trade is protected on CoinDCX itself (works even when the home server is off):
  * sl2 is a whole-position stop_market, and the LAST take profit is a whole-position
    take_profit_market, both via create_tpsl, placed the moment any quantity is filled.
  * Earlier (partial) take profits and the sl1 timer run on the home server. They are sent as
    market orders only after checking the position, because CoinDCX has no reduce-only flag.
"""
import logging
import time
from abc import ABC, abstractmethod

from .coindcx_futures import CoinDCXError
from .plan import Instrument, TradePlan

log = logging.getLogger("strata.trades")


class Executor(ABC):
    mode = "paper"

    def __init__(self, client, inr_per_usdt_override=None):
        self.client = client
        self.inr_override = inr_per_usdt_override
        self._inst, self._rate = {}, (0.0, 0.0)

    # ---------- market data (same in both modes) ----------
    def ticks(self, pair):
        """Recent trades as [(timestamp_ms, price)], oldest first."""
        rows = self.client.trades(pair)
        return sorted((int(float(t["timestamp"])), float(t["price"])) for t in rows)

    def instrument(self, plan: TradePlan) -> Instrument:
        key = (plan.pair, plan.margin_currency)
        cached = self._inst.get(key)
        if not cached or time.time() - cached[0] > 3600:
            raw = self.client.instrument(plan.pair, plan.margin_currency)
            cached = (time.time(), Instrument.from_api(plan.pair, raw))
            self._inst[key] = cached
        return cached[1]

    def inr_per_usdt(self):
        if time.time() - self._rate[0] > 600:
            self._rate = (time.time(), self._fetch_rate())
        return self._rate[1]

    @abstractmethod
    def _fetch_rate(self): ...

    # ---------- trading ----------
    def preflight(self, plan):
        """Checks and settings before the entry order (live: pair must be flat, leverage set)."""

    @abstractmethod
    def market_open(self, plan, qty, price_hint):
        """Returns (filled_qty, avg_price)."""

    @abstractmethod
    def limit_open(self, plan, qty, price):
        """Returns the order id."""

    @abstractmethod
    def entry_progress(self, plan, st, new_ticks):
        """Returns (filled_qty, avg_price, order_still_open)."""

    @abstractmethod
    def cancel_entry(self, plan, st): ...

    def recover_entry(self, plan, st):
        """After a crash between sending the entry and saving state: (filled, avg, open_order_id)."""
        return 0.0, 0.0, ""

    def protect(self, plan, st):
        """Put sl2 and the last take profit on the exchange. Paper: nothing to do."""

    @abstractmethod
    def reduce(self, plan, st, qty, price_hint):
        """Close part of the position at market. Returns the fill price."""

    @abstractmethod
    def close(self, plan, st, price_hint):
        """Close the whole position at market and cancel leftover orders. Returns the fill price."""

    def position(self, plan, st):
        """(open_qty, avg_price, liquidation_price) as the exchange sees it. Paper: Strata's own book."""
        return st.qty, st.entry_price, None


class PaperExecutor(Executor):
    mode = "paper"

    def _fetch_rate(self):
        if self.inr_override:
            return float(self.inr_override)
        return self.client.spot_inr_per_usdt()

    def market_open(self, plan, qty, price_hint):
        return qty, price_hint

    def limit_open(self, plan, qty, price):
        return f"paper-{plan.id}"

    def entry_progress(self, plan, st, new_ticks):
        d = plan.direction
        if any(d * price <= d * plan.entry for _, price in new_ticks):   # traded at or through our limit
            return st.ordered_qty, plan.entry, False
        return 0.0, 0.0, True

    def cancel_entry(self, plan, st):
        pass

    def reduce(self, plan, st, qty, price_hint):
        return price_hint

    def close(self, plan, st, price_hint):
        return price_hint


class LiveExecutor(Executor):
    mode = "live"

    def __init__(self, client, inr_per_usdt_override=None):
        super().__init__(client, inr_per_usdt_override)
        self._pos_cache = {}

    def _fetch_rate(self):
        return self.client.conversion_inr_per_usdt()      # the rate CoinDCX itself uses for INR margin

    # ---------- position helpers ----------
    def _record(self, plan, fresh=False):
        key = (plan.pair, plan.margin_currency)
        hit = self._pos_cache.get(key)
        if fresh or not hit or time.time() - hit[0] > 2.5:      # routine checks: every 2.5 s is plenty
            hit = (time.time(), self.client.position_record(plan.pair, plan.margin_currency))
            self._pos_cache[key] = hit
        return hit[1]

    def _signed_qty(self, rec):
        return float((rec or {}).get("active_pos") or 0)

    def position(self, plan, st):
        rec = self._record(plan)
        q = self._signed_qty(rec)
        if q * plan.direction <= 0:                 # flat (or, wrongly, the other way round)
            return 0.0, 0.0, None
        liq = float(rec.get("liquidation_price") or 0) or None
        return abs(q), float(rec.get("avg_price") or 0), liq

    def _wait_for(self, plan, predicate, seconds=10):
        for _ in range(seconds * 2):
            rec = self._record(plan, fresh=True)
            if predicate(self._signed_qty(rec)):
                return rec
            time.sleep(0.5)
        return None

    # ---------- entry ----------
    def preflight(self, plan):
        rec = self._record(plan, fresh=True)
        if rec and self._signed_qty(rec) != 0:
            raise CoinDCXError(f"{plan.pair} already has an open position on CoinDCX; close it first")
        if rec and float(rec.get("inactive_pos_buy") or 0) + float(rec.get("inactive_pos_sell") or 0) > 0:
            raise CoinDCXError(f"{plan.pair} has open orders on CoinDCX; cancel them first")
        if plan.margin_currency == "USDT" and rec and rec.get("margin_type") not in (None, "isolated"):
            self.client.set_margin_type(plan.pair, "isolated")
        if not rec or rec.get("leverage") is None or int(float(rec["leverage"])) != plan.leverage:
            self.client.update_leverage(plan.pair, plan.leverage, plan.margin_currency)

    def market_open(self, plan, qty, price_hint):
        self.client.create_order(plan.pair, plan.open_side, "market_order", qty, plan.leverage, plan.margin_currency)
        rec = self._wait_for(plan, lambda q: q * plan.direction > 0)
        if not rec:
            raise CoinDCXError(f"{plan.id}: market order sent but no position appeared in 10 s; check CoinDCX")
        return abs(self._signed_qty(rec)), float(rec.get("avg_price") or price_hint)

    def limit_open(self, plan, qty, price):
        inst = self.instrument(plan)
        order = self.client.create_order(plan.pair, plan.open_side, "limit_order", qty, plan.leverage,
                                         plan.margin_currency, price=inst.fmt_price(price))
        return str(order["id"])

    def _open_orders(self, plan):
        return self.client.list_orders(plan.open_side, "open,partially_filled", plan.margin_currency)

    def entry_progress(self, plan, st, new_ticks):
        rec = self._record(plan, fresh=True)
        q = self._signed_qty(rec)
        filled = abs(q) if q * plan.direction > 0 else 0.0
        avg = float((rec or {}).get("avg_price") or 0)
        still_open = any(str(o.get("id")) == st.entry_order_id for o in self._open_orders(plan))
        return filled, avg, still_open

    def cancel_entry(self, plan, st):
        try:
            self.client.cancel_order(st.entry_order_id)
        except CoinDCXError as e:
            log.warning("%s: cancel entry order: %s", plan.id, e)     # already filled or cancelled

    def recover_entry(self, plan, st):
        rec = self._record(plan, fresh=True)
        q = self._signed_qty(rec)
        filled = abs(q) if q * plan.direction > 0 else 0.0
        orders = [o for o in self._open_orders(plan) if o.get("pair") == plan.pair]
        return filled, float((rec or {}).get("avg_price") or 0), str(orders[0]["id"]) if orders else ""

    # ---------- protection and exits ----------
    def protect(self, plan, st):
        inst = self.instrument(plan)
        rec = self._record(plan, fresh=True)
        if not rec or self._signed_qty(rec) == 0:
            raise CoinDCXError(f"{plan.id}: no position to protect")
        self.client.create_tpsl(rec["id"], take_profit=inst.fmt_price(plan.take_profits[-1].price),
                                stop_loss=inst.fmt_price(st.sl2))

    def reduce(self, plan, st, qty, price_hint):
        inst = self.instrument(plan)
        held, _, _ = self.position(plan, st)
        qty = inst.round_qty(min(qty, held))
        if qty <= 0:
            return price_hint
        self.client.create_order(plan.pair, plan.close_side, "market_order", qty, plan.leverage, plan.margin_currency)
        time.sleep(1)
        rec = self._record(plan, fresh=True)
        if self._signed_qty(rec) * plan.direction < 0:
            # The exchange closed the position at the same moment, so our reduce opened the other way.
            log.critical("%s: partial take profit flipped the position; closing it", plan.id)
            self.client.exit_position(rec["id"])
        return self.client.last_price(plan.pair)

    def close(self, plan, st, price_hint):
        rec = self._record(plan, fresh=True)
        if rec and self._signed_qty(rec) != 0:
            self.client.exit_position(rec["id"])
            if not self._wait_for(plan, lambda q: q == 0):
                raise CoinDCXError(f"{plan.id}: exit sent but the position is still open; will retry")
        if rec:
            try:
                self.client.cancel_position_orders(rec["id"])      # no orphan orders left behind
            except CoinDCXError as e:
                log.warning("%s: cancel leftover orders: %s", plan.id, e)
        try:
            return self.client.last_price(plan.pair)
        except Exception:
            return price_hint
