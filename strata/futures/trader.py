"""Timed-stop trade manager for CoinDCX futures (service: strata@coindcx_trades).

Runs every trade in config/trades.yaml side by side (one active trade per pair). Each trade goes:

    new -> [entering] -> entry_working (limit only) -> open -> closed
                    \\-> rejected / cancelled / failed (with a note saying why)

While open, every price tick goes through:
  1. take profits, nearest first: partial closes; the last one closes everything
  2. the sl1/sl2 timed stop (stops.py): timer start / reset, close at sl2, close when the timer runs out

State is saved to state/trades_state.json after every step, so a restart (power cut, crash, update)
continues where it left off, timer included. Once a trade has started, its plan is frozen in the
state file: editing config/trades.yaml can't change or orphan a trade that is already running.

To extend: on_take_profit() below is the place for "move the stop after a take profit".
"""
import csv
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ..state import load_json, save_json
from .plan import PlanError, TradePlan, load_plans
from .stops import HIT_SL2, TIMER_EXPIRED, TIMER_RESET, TIMER_STARTED, TimedStop

log = logging.getLogger("strata.trades")

ACTIVE = ("entering", "entry_working", "open")
FINAL = ("closed", "cancelled", "rejected", "failed", "invalid")
# invalid  = the config entry itself is wrong; fixing it (same id) lets it run
# rejected = refused at start time (price, size, busy pair...); never retried by itself


@dataclass
class TradeState:
    phase: str = "new"
    note: str = ""
    plan: dict = field(default_factory=dict)      # frozen copy of the plan once the trade starts
    entry_order_id: str = ""
    entry_placed_at: float = 0.0
    ordered_qty: float = 0.0
    qty: float = 0.0                              # currently open
    entry_price: float = 0.0
    sl1: float = 0.0                              # live stop levels (start as the plan's; a future
    sl2: float = 0.0                              # "move stop" feature changes these)
    tp_qty: list = field(default_factory=list)
    tp_done: list = field(default_factory=list)
    protected: bool = False                       # sl2 + last TP placed on the exchange
    timer_started_at: float | None = None
    last_tick_ms: int = 0
    inr_rate: float = 0.0                         # INR per USDT used to size an INR-margin trade
    realized_pnl: float = 0.0                     # USDT, after fees
    fees: float = 0.0
    opened_at: str = ""
    closed_at: str = ""


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


class TradeManager:
    def __init__(self, plans, executor, state_dir: Path, log_dir: Path, rejected=None):
        self.ex = executor
        self.mode = executor.mode
        self.state_path = Path(state_dir) / "trades_state.json"
        self.csv_path = Path(log_dir) / "trades_coindcx_trades.csv"
        raw = load_json(self.state_path, {})
        self.state = {tid: TradeState(**s) for tid, s in raw.items()}
        self.plans = {}
        # running trades keep their frozen plan, whatever the config says now
        for tid, st in self.state.items():
            if st.phase in ACTIVE and st.plan:
                self.plans[tid] = TradePlan(**st.plan)
        for p in plans:
            if p.id in self.plans:
                continue
            self.plans[p.id] = p
            st = self.state.setdefault(p.id, TradeState())
            if st.phase == "invalid":
                self.state[p.id] = TradeState()          # the config was fixed: start fresh
            elif st.phase in FINAL:
                log.info("%s already finished (%s); give a new trade a new id", p.id, st.phase)
        for tid, err in (rejected or {}).items():
            st = self.state.get(tid)
            if st is None or st.phase in ("new", "invalid"):
                self.state[tid] = TradeState(phase="invalid", note=err)
        self.save()

    # ================= bookkeeping =================
    def save(self):
        save_json(self.state_path, {k: asdict(v) for k, v in self.state.items()})

    def record(self, plan, event, qty, price, pnl=None, fee=None, note=""):
        new = not self.csv_path.exists()
        with self.csv_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "mode", "trade", "pair", "side", "event", "qty", "price", "pnl_usdt", "fee_usdt", "note"])
            w.writerow([_now_iso(), self.mode, plan.id, plan.pair, plan.side, event, qty, price,
                        "" if pnl is None else round(pnl, 4), "" if fee is None else round(fee, 4), note])
        log.info("TRADE [%s] %s %s %s %s @ %s pnl=%s %s", self.mode, plan.id, event, qty, plan.pair, price,
                 "" if pnl is None else round(pnl, 4), note)

    def finish(self, plan, st, phase, note):
        st.phase, st.note = phase, note
        st.closed_at = st.closed_at or _now_iso()
        st.timer_started_at = None
        log.info("%s: %s (%s)", plan.id, phase, note)

    # ================= main loop step =================
    def tick(self, now=None):
        now = now or time.time()
        todo = [(tid, st) for tid, st in self.state.items()
                if tid in self.plans and st.phase not in FINAL
                and not (st.phase == "new" and not self.plans[tid].enabled)]
        ticks = {}
        for pair in {self.plans[tid].pair for tid, _ in todo}:
            try:
                ticks[pair] = self.ex.ticks(pair)
            except Exception as e:
                log.warning("no prices for %s this round: %s", pair, e)
        for tid, st in todo:
            plan = self.plans[tid]
            if plan.pair not in ticks:
                continue
            try:
                self.step(plan, st, ticks[plan.pair], now)
            except Exception as e:
                log.exception("%s: error in phase %s", tid, st.phase)
                if st.phase in ("new", "entering"):
                    # never retry an entry automatically: a repeated order could double the position
                    self.finish(plan, st, "failed", f"entry failed: {e}")
                else:
                    st.note = f"error (will retry): {e}"
            finally:
                self.save()
        return ticks

    def step(self, plan, st, all_ticks, now):
        new_ticks = [(t, p) for t, p in all_ticks if t > st.last_tick_ms]
        if st.phase == "new":
            return self.start(plan, st, all_ticks, now)
        if st.phase == "entering":
            return self.recover(plan, st, now)
        if st.phase == "entry_working":
            self.work_entry(plan, st, new_ticks, now)
        elif st.phase == "open":
            self.manage(plan, st, new_ticks, now)
        if all_ticks:
            st.last_tick_ms = max(st.last_tick_ms, all_ticks[-1][0])

    # ================= entry =================
    def start(self, plan, st, all_ticks, now):
        if not plan.enabled:
            return
        busy = [tid for tid, s in self.state.items() if tid != plan.id and s.phase in ACTIVE
                and self.plans.get(tid) and self.plans[tid].pair == plan.pair]
        if busy:
            return self.finish(plan, st, "rejected", f"{plan.pair} already has an active trade ({busy[0]})")
        if not all_ticks:
            return
        price = all_ticks[-1][1]
        st.last_tick_ms = all_ticks[-1][0]
        inst = self.ex.instrument(plan)
        try:
            entry_ref = price if plan.is_market else plan.entry
            problem = plan.entry_problem(entry_ref, inst.mmr)
            if not problem and not plan.is_market and plan.direction * price <= plan.direction * plan.entry:
                # the limit is already through the market, so it fills now at about this price
                problem = plan.entry_problem(price, inst.mmr)
            if problem:
                raise PlanError(problem)
            if plan.leverage > inst.max_leverage:
                raise PlanError(f"{plan.pair} allows at most {inst.max_leverage:g}x")
            rate = self.ex.inr_per_usdt() if plan.margin_currency == "INR" else None
            qty = plan.quantity(entry_ref, inst, rate)
            plan.split(qty, inst, strict=True)
            self.ex.preflight(plan)
        except Exception as e:
            return self.finish(plan, st, "rejected", str(e))

        st.plan = asdict(plan)
        st.inr_rate = rate or 0.0
        st.ordered_qty = qty
        st.phase = "entering"
        self.save()                                   # saved BEFORE the order: a crash can't cause a double entry
        if plan.is_market:
            filled, avg = self.ex.market_open(plan, qty, price)
            self.entry_done(plan, st, filled, avg, now, maker=False)
        else:
            st.entry_order_id = self.ex.limit_open(plan, qty, plan.entry)
            st.entry_placed_at = now
            st.phase = "entry_working"
            self.record(plan, "entry_order", qty, plan.entry,
                        note=f"limit, cancels after {plan.entry_expiry_minutes:g} min if not filled")

    def recover(self, plan, st, now):
        """We crashed after deciding to enter. Look at the exchange; never send the entry again."""
        filled, avg, order_id = self.ex.recover_entry(plan, st)
        if order_id:
            st.entry_order_id, st.entry_placed_at, st.phase = order_id, now, "entry_working"
        elif filled > 0:
            self.entry_done(plan, st, filled, avg, now, maker=not plan.is_market)
        else:
            self.finish(plan, st, "failed", "entry was interrupted and nothing is on CoinDCX; not retried")

    def work_entry(self, plan, st, new_ticks, now):
        filled, avg, still_open = self.ex.entry_progress(plan, st, new_ticks)
        if filled > 0 and not st.protected:
            st.entry_price = st.entry_price or avg
            if not self.protect(plan, st, filled, entry_still_open=still_open):
                return                              # couldn't protect: entry cancelled and position closed
        if not still_open:
            if filled > 0:
                return self.entry_done(plan, st, filled, avg, now, maker=True)
            return self.finish(plan, st, "cancelled", "entry order was cancelled outside Strata")
        if now - st.entry_placed_at >= plan.entry_expiry_minutes * 60:
            self.ex.cancel_entry(plan, st)
            filled, avg, _ = self.ex.entry_progress(plan, st, [])
            if filled > 0:
                return self.entry_done(plan, st, filled, avg, now, maker=True,
                                       note=f"partly filled ({filled:g}) before the {plan.entry_expiry_minutes:g} min expiry")
            return self.finish(plan, st, "cancelled", f"limit not filled within {plan.entry_expiry_minutes:g} min")

    def entry_done(self, plan, st, filled, avg, now, maker, note=""):
        inst = self.ex.instrument(plan)
        st.qty, st.entry_price = filled, avg
        st.sl1, st.sl2 = plan.sl1, plan.sl2
        st.tp_qty = plan.split(filled, inst, strict=False)
        st.tp_done = [q <= 0 for q in st.tp_qty]
        st.timer_started_at = None
        st.phase, st.opened_at, st.note = "open", _now_iso(), note
        fee = filled * avg * (inst.maker_fee_pct if maker else inst.taker_fee_pct) / 100
        st.fees += fee
        st.realized_pnl -= fee
        self.record(plan, "entry", filled, avg, pnl=-fee, fee=fee,
                    note=(note or ("market" if plan.is_market else "limit filled"))
                    + f"; TPs {'/'.join(f'{q:g}' for q in st.tp_qty)}; sl1 {st.sl1:g} sl2 {st.sl2:g}"
                    + (f"; INR/USDT {st.inr_rate:g}" if st.inr_rate else ""))
        if not st.protected and not self.protect(plan, st, filled):
            return
        _, _, liq = self.ex.position(plan, st)
        if liq and plan.direction * liq >= plan.direction * st.sl2:
            log.critical("%s: liquidation price %s is at or before sl2 %s: the exchange would liquidate "
                         "before sl2 fires. Lower the leverage on the next trade.", plan.id, liq, st.sl2)
            st.note = f"WARNING: liquidation {liq} before sl2"

    def protect(self, plan, st, filled, entry_still_open=False):
        """Put sl2 (and the last TP) on the exchange. If that fails, close: never hold an unprotected trade."""
        try:
            st.sl1, st.sl2 = st.sl1 or plan.sl1, st.sl2 or plan.sl2
            self.ex.protect(plan, st)
            st.protected = True
            return True
        except Exception as e:
            log.exception("%s: could not place sl2 on CoinDCX; closing so the trade is never unprotected", plan.id)
            if entry_still_open:
                self.ex.cancel_entry(plan, st)
            st.qty = st.qty or filled
            self.close(plan, st, st.entry_price, f"could not place stop on CoinDCX: {e}")
            return False

    # ================= open trade =================
    def manage(self, plan, st, new_ticks, now):
        # 1) did the exchange close (sl2 / last TP fired) or shrink the position while we weren't looking?
        held, _, _ = self.ex.position(plan, st)
        if held <= 0:
            return self.closed_outside(plan, st, new_ticks)
        if held < st.qty - 1e-12:
            log.warning("%s: position shrank outside Strata (%g -> %g)", plan.id, st.qty, held)
            st.qty = held

        # 2) every tick, in order
        stop = TimedStop(plan.side, st.sl1, st.sl2, plan.sl1_timer_minutes * 60)
        price = None
        for ts_ms, price in new_ticks:
            if self.take_profits(plan, st, price):
                return
            started, event = stop.on_tick(st.timer_started_at, price, ts_ms / 1000)
            if event == TIMER_STARTED:
                log.info("%s: price %s reached sl1 %s; %g min timer started", plan.id, price, st.sl1,
                         plan.sl1_timer_minutes)
            elif event == TIMER_RESET:
                log.info("%s: price %s back above sl1 %s; timer reset", plan.id, price, st.sl1)
            st.timer_started_at = started
            if event == HIT_SL2:
                return self.close(plan, st, price, f"sl2 {st.sl2:g} reached")
            if event == TIMER_EXPIRED:
                return self.close(plan, st, price, f"sl1 timer ({plan.sl1_timer_minutes:g} min) ran out")
            st.last_tick_ms = ts_ms

        # 3) the timer also runs out between ticks
        if stop.check(st.timer_started_at, now) == TIMER_EXPIRED:
            last = price if price is not None else self.ex.client.last_price(plan.pair)
            self.close(plan, st, last, f"sl1 timer ({plan.sl1_timer_minutes:g} min) ran out")

    def take_profits(self, plan, st, price):
        """Returns True if the trade is now closed."""
        last = len(plan.take_profits) - 1
        for i, tp in enumerate(plan.take_profits):
            if st.tp_done[i]:
                continue
            if not plan.reached(price, tp.price):
                return False                       # take profits are in order: nothing further is hit
            remaining_after = [j for j in range(i + 1, last + 1) if not st.tp_done[j]]
            if not remaining_after or plan.reached(price, plan.take_profits[-1].price):
                # last take profit (or price already past it): close everything in one go
                self.close(plan, st, price, f"take profit {last + 1} ({plan.take_profits[-1].price:g})")
                st.tp_done = [True] * len(st.tp_done)
                return True
            qty = min(st.tp_qty[i], st.qty)
            fill = self.ex.reduce(plan, st, qty, price)
            inst = self.ex.instrument(plan)
            fee = qty * fill * inst.taker_fee_pct / 100
            pnl = plan.direction * (fill - st.entry_price) * qty - fee
            st.qty = round(st.qty - qty, 10)
            st.tp_done[i] = True
            st.realized_pnl += pnl
            st.fees += fee
            self.record(plan, f"tp{i + 1}", qty, fill, pnl=pnl, fee=fee,
                        note=f"take profit {i + 1} at {tp.price:g} ({tp.percent:g}%)")
            self.on_take_profit(plan, st, i)
        return False

    def on_take_profit(self, plan, st, index):
        """Hook after take profit #index (0-based) fills. Currently does nothing.

        Future "move the stop after a take profit": change st.sl1 / st.sl2 here, e.g.
            st.sl1 = st.entry_price            # breakeven
        and, if sl2 moved, update the exchange stop with self.ex.protect(plan, st) (on CoinDCX the
        existing whole-position stop has to be cancelled first). The timed stop reads st.sl1 / st.sl2
        on every tick, so nothing else needs to change.
        """

    def close(self, plan, st, price_hint, reason):
        qty = st.qty
        fill = self.ex.close(plan, st, price_hint)
        inst = self.ex.instrument(plan)
        fee = qty * fill * inst.taker_fee_pct / 100
        pnl = plan.direction * (fill - st.entry_price) * qty - fee
        st.realized_pnl += pnl
        st.fees += fee
        st.qty = 0.0
        self.record(plan, "close", qty, fill, pnl=pnl, fee=fee, note=reason)
        self.finish(plan, st, "closed", f"{reason}; total P&L {st.realized_pnl:+.4f} USDT")

    def closed_outside(self, plan, st, new_ticks):
        """The position is gone: CoinDCX's sl2 or last-TP order fired (e.g. while we were offline)."""
        price = new_ticks[-1][1] if new_ticks else None
        d, last_tp = plan.direction, plan.take_profits[-1].price
        if price is not None and d * price <= d * st.sl1:
            est, why = st.sl2, "sl2 fired on CoinDCX"
        elif price is not None and (plan.reached(price, last_tp) or abs(price - last_tp) / last_tp < 0.003):
            est, why = last_tp, "last take profit fired on CoinDCX"
        else:
            est, why = (price or st.entry_price), "position closed outside Strata"
        qty = st.qty
        pnl = d * (est - st.entry_price) * qty
        st.realized_pnl += pnl
        st.qty = 0.0
        try:
            self.ex.close(plan, st, est)               # cancels any leftover orders for the position
        except Exception as e:
            log.warning("%s: cleanup after outside close: %s", plan.id, e)
        self.record(plan, "close", qty, est, pnl=pnl, note=f"{why} (price estimated)")
        self.finish(plan, st, "closed", f"{why}; total P&L ~{st.realized_pnl:+.4f} USDT")

    # ================= reporting =================
    def status(self, now=None, ticks=None):
        """One line per running trade, for the log every 5 minutes."""
        now = now or time.time()
        out = []
        for tid, st in self.state.items():
            if st.phase not in ACTIVE:
                continue
            plan = self.plans[tid]
            line = f"{tid}: {st.phase}"
            if st.phase == "open":
                stop = TimedStop(plan.side, st.sl1, st.sl2, plan.sl1_timer_minutes * 60)
                left = stop.seconds_left(st.timer_started_at, now)
                line += (f" {plan.side} {st.qty:g} @ {st.entry_price:g}, sl1 {st.sl1:g} sl2 {st.sl2:g}, "
                         f"TPs done {sum(st.tp_done)}/{len(st.tp_done)}")
                line += f", TIMER {left:.0f}s left" if left is not None else ""
            recent = (ticks or {}).get(plan.pair)
            if recent:
                line += f", last price {recent[-1][1]:g}"
            out.append(line)
        return out


def run(root: Path, env: dict):
    """Entry point: main.py --platform coindcx_trades."""
    import yaml

    from ..healthcheck import ping
    from .coindcx_futures import CoinDCXFutures
    from .executors import LiveExecutor, PaperExecutor

    path = root / "config" / "trades.yaml"                 # the server's own file, not in git
    if not path.exists():
        path = root / "config" / "trades.example.yaml"
        log.warning("config/trades.yaml not found; using the example file (copy it to start trading)")
    cfg = yaml.safe_load(path.read_text()) or {}
    mode = env.get("TRADING_MODE", "paper").lower()
    client = CoinDCXFutures(env.get("COINDCX_API_KEY", ""), env.get("COINDCX_API_SECRET", ""),
                            notification=cfg.get("notification", "email_notification"))
    executor = (LiveExecutor if mode == "live" else PaperExecutor)(client, cfg.get("inr_per_usdt"))
    plans, rejected = load_plans(cfg.get("trades"), log)
    mgr = TradeManager(plans, executor, root / "state", root / "logs", rejected)
    hc = env.get("HC_PING_COINDCX_TRADES")
    poll = float(cfg.get("poll_seconds", 1))
    log.info("CoinDCX timed-stop trader starting in %s mode with %d trade(s) in config", mode.upper(), len(plans))
    for tid, st in mgr.state.items():
        log.info("  %s: %s %s", tid, st.phase, st.note)
    last_status, last_ping, failures = 0.0, 0.0, 0
    while True:
        try:
            ticks = mgr.tick()
            failures = 0
            now = time.time()
            if now - last_status >= 300:
                for line in mgr.status(now, ticks) or ["no active trades"]:
                    log.info("status: %s", line)
                last_status = now
            if now - last_ping >= 60:
                ping(hc)
                last_ping = now
        except Exception as e:
            failures += 1
            log.exception("loop failed (%d in a row)", failures)
            if failures in (5, 60, 600):
                ping(hc, "fail", repr(e))
        time.sleep(poll)
