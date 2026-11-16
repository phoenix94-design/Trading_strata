"""One loop = reconcile, enforce stops, then let the strategy act on each newly closed candle."""
import csv
import logging
import time
from datetime import datetime, time as dtime

from . import strategies
from .healthcheck import ping
from .risk import daily_loss_hit, kill_switch_on, position_size
from .state import load_json, save_json

log = logging.getLogger("strata")
NSE_OPEN, NSE_CLOSE = dtime(9, 15), dtime(15, 30)


class Engine:
    def __init__(self, settings, broker, data):
        self.s, self.broker, self.data = settings, broker, data
        sc = settings.cfg["strategy"]
        self.strategy = strategies.build(sc["name"], sc.get("params"))
        self.interval = settings.cfg["interval"]
        self.state_path = settings.state_dir / f"engine_{settings.platform}.json"
        self.state = load_json(self.state_path, {"last_candle": {}, "day": "", "day_start_equity": 0})
        self.hc_url = settings.env.get(f"HC_PING_{settings.platform.upper()}")

    # ---------- helpers ----------
    def market_open(self, now=None):
        if self.s.platform != "zerodha":
            return True                            # crypto: 24/7
        now = now or datetime.now()
        return now.weekday() < 5 and NSE_OPEN <= now.time() <= NSE_CLOSE

    def equity(self):
        return self.broker.cash() + sum(p.qty * self.data.last_price(p.symbol)
                                        for p in self.broker.positions().values())

    def record(self, side, symbol, qty, price, reason, entry=None):
        path = self.s.log_dir / f"trades_{self.s.platform}.csv"
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "mode", "side", "symbol", "qty", "price", "pnl", "reason"])
            pnl = "" if entry is None else round((price - entry) * qty, 2)
            w.writerow([datetime.now().isoformat(timespec="seconds"), self.s.mode, side, symbol, qty,
                        round(price, 4), pnl, reason])
        log.info("TRADE %s %s %s @ %.4f pnl=%s (%s)", side, qty, symbol, price, pnl, reason)

    # ---------- the loop ----------
    def tick(self):
        if not self.market_open():
            return "market closed"
        if hasattr(self.broker, "ready") and not self.broker.ready():
            ping(self.hc_url, "fail", "Kite access token missing: run python -m strata.zerodha_login")
            return "waiting for Kite login"

        positions_before = self.broker.positions()
        for sym, price in self.broker.sync():
            self.record("sell", sym, positions_before[sym].qty, price, "closed at broker (stop/GTT or manual)",
                        positions_before[sym].entry)
        positions_before = self.broker.positions()
        for sym, price in self.broker.check_stops():
            self.record("sell", sym, positions_before[sym].qty, price, "stop hit", positions_before[sym].entry)

        equity = self.equity()
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state["day"] != today:
            self.state.update(day=today, day_start_equity=equity)
        blocked = None
        if kill_switch_on(self.s.state_dir):
            blocked = "kill switch (state/KILL) is on"
        elif daily_loss_hit(self.state["day_start_equity"], equity, self.s.risk):
            blocked = f"daily loss limit {self.s.risk.max_daily_loss_pct}% reached"

        for symbol in self.s.symbols:
            try:
                self.handle(symbol, equity, blocked)
            except Exception:
                log.exception("error on %s", symbol)
        save_json(self.state_path, self.state)
        return f"ok equity={equity:.2f}" + (f" [entries blocked: {blocked}]" if blocked else "")

    def handle(self, symbol, equity, blocked):
        candles = self.data.candles(symbol, self.interval, self.strategy.warmup + 50)
        if not candles or self.state["last_candle"].get(symbol) == candles[-1].ts:
            return                                  # nothing new closed yet
        pos = self.broker.positions().get(symbol)
        sig = self.strategy.decide(candles, pos)
        log.info("%s %s close=%.4f -> %s (%s)", symbol, datetime.fromtimestamp(candles[-1].ts),
                 candles[-1].close, sig.action, sig.reason)

        if sig.action == "buy" and pos is None:
            if blocked:
                log.info("skip buy %s: %s", symbol, blocked)
            elif len(self.broker.positions()) >= self.s.risk.max_open_positions:
                log.info("skip buy %s: max open positions", symbol)
            else:
                step, min_qty = self.data.lot(symbol)
                price = self.data.last_price(symbol)
                qty = position_size(equity, self.broker.cash(), price, sig.stop, self.s.risk, step, min_qty)
                if qty > 0:
                    p = self.broker.buy(symbol, qty, sig.stop)
                    self.record("buy", symbol, qty, p.entry, f"{sig.reason}; stop {sig.stop:.4f}")
                else:
                    log.info("skip buy %s: size rounds to 0 (account too small for this stop)", symbol)
        elif sig.action == "sell" and pos is not None:
            price = self.broker.sell(symbol, sig.reason)
            self.record("sell", symbol, pos.qty, price, sig.reason, pos.entry)
        elif pos is not None and sig.stop and sig.stop > pos.stop:
            self.broker.move_stop(symbol, sig.stop)
            log.info("%s stop raised %.4f -> %.4f", symbol, pos.stop, sig.stop)

        self.state["last_candle"][symbol] = candles[-1].ts

    def run_forever(self, poll_seconds):
        log.info("Strata %s starting in %s mode, symbols=%s, interval=%s",
                 self.s.platform, self.s.mode.upper(), self.s.symbols, self.interval)
        while True:
            try:
                status = self.tick()
                log.debug(status)
                if status != "waiting for Kite login":
                    ping(self.hc_url)              # only after a loop that completed
            except Exception as e:
                log.exception("loop failed")
                ping(self.hc_url, "fail", repr(e))
            time.sleep(poll_seconds)
