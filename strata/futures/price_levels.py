"""Price-level strategy for CoinDCX futures.

You set the prices; the bot watches the market 24/7 and acts on them:

    waiting --(price reaches entry_price)--> open --(take_profit or stop_loss hit)--> done
                                                                                      (or back to
                                                                                       waiting if
                                                                                       repeat: true)

* Long  (side: long):  buy at entry, sell at take_profit (above) or stop_loss (below).
* Short (side: short): sell at entry, buy back at take_profit (below) or stop_loss (above).
* Live: the moment a position opens, take-profit and stop-loss are placed ON COINDCX
  (create_tpsl), so they fire even if the home server is off. If that fails, the position is
  closed straight away: a leveraged position is never left without a stop.
* Paper (TRADING_MODE=paper, the default): same logic, simulated fills, real prices.
"""
import csv
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ..state import load_json, save_json

log = logging.getLogger("strata.futures")

MAX_LEVERAGE = 10          # hard cap, whatever the config says
BACKUP_STOP_SLIPPAGE = 0.005   # if price is 0.5% past the stop and the position is still open, close it ourselves


@dataclass
class Setup:
    name: str
    pair: str                   # CoinDCX futures pair, e.g. B-BTC_USDT
    side: str                   # long | short
    entry_price: float
    take_profit: float
    stop_loss: float
    quantity: float             # contract size in the coin, e.g. 0.001 BTC
    leverage: int = 1
    entry_when: str = ""        # at_or_below | at_or_above; default: below for long, above for short
    repeat: bool = False        # after an exit, wait for entry_price again
    price_decimals: int = 2     # decimals CoinDCX accepts for this pair's prices
    enabled: bool = True

    def __post_init__(self):
        self.side = self.side.lower()
        if self.side not in ("long", "short"):
            raise ValueError(f"{self.name}: side must be long or short")
        if not self.entry_when:
            self.entry_when = "at_or_below" if self.side == "long" else "at_or_above"
        if self.entry_when not in ("at_or_below", "at_or_above"):
            raise ValueError(f"{self.name}: entry_when must be at_or_below or at_or_above")
        if self.side == "long" and not self.stop_loss < self.entry_price < self.take_profit:
            raise ValueError(f"{self.name}: long needs stop_loss < entry_price < take_profit")
        if self.side == "short" and not self.take_profit < self.entry_price < self.stop_loss:
            raise ValueError(f"{self.name}: short needs take_profit < entry_price < stop_loss")
        if not 1 <= self.leverage <= MAX_LEVERAGE:
            raise ValueError(f"{self.name}: leverage must be 1..{MAX_LEVERAGE}")
        if self.quantity <= 0:
            raise ValueError(f"{self.name}: quantity must be > 0")
        # With isolated margin you are liquidated after roughly a 1/leverage move.
        # The stop must sit well inside that, or liquidation hits before the stop does.
        stop_move = abs(self.entry_price - self.stop_loss) / self.entry_price
        if stop_move >= 0.8 / self.leverage:
            raise ValueError(f"{self.name}: stop is {stop_move:.1%} away but {self.leverage}x leverage "
                             f"liquidates near {1 / self.leverage:.1%}; lower leverage or tighten the stop")

    @property
    def order_side(self):          # side of the opening order
        return "buy" if self.side == "long" else "sell"

    def entry_hit(self, price):
        return price <= self.entry_price if self.entry_when == "at_or_below" else price >= self.entry_price

    def tp_hit(self, price):
        return price >= self.take_profit if self.side == "long" else price <= self.take_profit

    def sl_hit(self, price, slack=0.0):
        if self.side == "long":
            return price <= self.stop_loss * (1 - slack)
        return price >= self.stop_loss * (1 + slack)

    def pnl(self, entry, exit_, qty):
        return (exit_ - entry) * qty if self.side == "long" else (entry - exit_) * qty

    def fmt(self, price):
        return f"{price:.{self.price_decimals}f}"


@dataclass
class SetupState:
    phase: str = "waiting"         # waiting | open | done
    entry_fill: float = 0.0
    position_id: str = ""
    opened_at: str = ""
    trades: int = 0
    realized_pnl: float = 0.0


class PriceLevelBot:
    def __init__(self, setups, client, mode, state_dir: Path, log_dir: Path, fee_pct=0.05):
        self.setups = [s for s in setups if s.enabled]
        self.client, self.mode, self.fee_pct = client, mode, fee_pct
        self.state_path = Path(state_dir) / "futures_price_levels.json"
        self.trade_log = Path(log_dir) / "trades_coindcx_futures.csv"
        raw = load_json(self.state_path, {})
        self.state = {s.name: SetupState(**raw.get(s.name, {})) for s in self.setups}
        self.reconciled = False

    # ---------- bookkeeping ----------
    def save(self):
        save_json(self.state_path, {k: asdict(v) for k, v in self.state.items()})

    def record(self, s: Setup, action, price, reason, pnl=None):
        new = not self.trade_log.exists()
        with self.trade_log.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "mode", "setup", "pair", "side", "action", "qty", "leverage", "price", "pnl_usdt", "reason"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), self.mode, s.name, s.pair, s.side, action,
                        s.quantity, s.leverage, s.fmt(price), "" if pnl is None else round(pnl, 4), reason])
        log.info("TRADE [%s] %s %s %s %s @ %s pnl=%s (%s)", self.mode, s.name, action, s.quantity, s.pair,
                 s.fmt(price), "" if pnl is None else round(pnl, 4), reason)

    def fees(self, *prices_qty):
        return sum(p * q for p, q in prices_qty) * self.fee_pct / 100

    # ---------- live helpers ----------
    def reconcile(self):
        """After a restart, trust the exchange: adopt positions we don't know about, close ones that are gone."""
        if self.mode != "live":
            return
        for s in self.setups:
            st, pos = self.state[s.name], self.client.open_position(s.pair)
            if pos and st.phase != "open":
                log.warning("%s: found an open %s position on CoinDCX; adopting it and (re)placing TP/SL", s.name, s.pair)
                st.phase, st.position_id = "open", str(pos["id"])
                st.entry_fill = float(pos.get("avg_price") or s.entry_price)
                self.client.set_tpsl(st.position_id, s.take_profit, s.stop_loss, s.fmt)
            elif not pos and st.phase == "open":
                self.close_book(s, self.client.last_price(s.pair), "closed while Strata was offline")
        self.save()

    def open_live(self, s: Setup, st: SetupState):
        self.client.market_order(s.pair, s.order_side, s.quantity, s.leverage)
        pos = None
        for _ in range(10):                       # the position can take a moment to show up
            time.sleep(1)
            pos = self.client.open_position(s.pair)
            if pos:
                break
        if not pos:
            raise RuntimeError(f"{s.name}: order sent but no position appeared; check CoinDCX by hand")
        st.position_id = str(pos["id"])
        st.entry_fill = float(pos.get("avg_price") or self.client.last_price(s.pair))
        try:
            self.client.set_tpsl(st.position_id, s.take_profit, s.stop_loss, s.fmt)
        except Exception:
            log.exception("%s: could not place TP/SL; closing the position so it is never unprotected", s.name)
            self.client.exit_position(st.position_id)
            raise

    # ---------- state transitions ----------
    def close_book(self, s: Setup, exit_price, reason):
        st = self.state[s.name]
        pnl = s.pnl(st.entry_fill, exit_price, s.quantity) - self.fees((st.entry_fill, s.quantity), (exit_price, s.quantity))
        self.record(s, "close", exit_price, reason, pnl)
        st.trades += 1
        st.realized_pnl += pnl
        st.phase = "waiting" if s.repeat else "done"
        st.position_id, st.entry_fill, st.opened_at = "", 0.0, ""

    def step(self, s: Setup, price):
        st = self.state[s.name]
        if st.phase == "done":
            return

        if st.phase == "waiting":
            if not s.entry_hit(price):
                return
            if s.sl_hit(price) or s.tp_hit(price):
                log.info("%s: price %s already past stop/target; not entering", s.name, s.fmt(price))
                return
            if self.mode == "live":
                self.open_live(s, st)
            else:
                st.entry_fill = price
            st.phase, st.opened_at = "open", datetime.now().isoformat(timespec="seconds")
            self.record(s, "open", st.entry_fill, f"price reached {s.fmt(s.entry_price)}; "
                                                  f"TP {s.fmt(s.take_profit)} SL {s.fmt(s.stop_loss)}")
            return

        # phase == "open"
        if self.mode == "paper":
            if s.tp_hit(price):
                self.close_book(s, s.take_profit, "take profit")
            elif s.sl_hit(price):
                self.close_book(s, s.stop_loss, "stop loss")
            return

        pos = self.client.open_position(s.pair)
        if pos is None:                          # CoinDCX's TP/SL fired
            reason = "take profit" if abs(price - s.take_profit) < abs(price - s.stop_loss) else "stop loss"
            self.close_book(s, s.take_profit if reason == "take profit" else s.stop_loss, f"{reason} (on CoinDCX)")
        elif s.sl_hit(price, slack=BACKUP_STOP_SLIPPAGE):
            log.warning("%s: price %s is past the stop but the position is still open; closing it", s.name, s.fmt(price))
            self.client.exit_position(st.position_id)
            self.close_book(s, price, "backup stop by Strata")

    def tick(self):
        if not self.reconciled:
            self.reconcile()
            self.reconciled = True
        prices = {}
        for s in self.setups:
            if self.state[s.name].phase == "done":
                continue
            if s.pair not in prices:
                prices[s.pair] = self.client.last_price(s.pair)
            try:
                self.step(s, prices[s.pair])
            finally:
                self.save()
        return prices


def load_setups(cfg):
    return [Setup(**x) for x in cfg.get("setups", [])]


def run(root: Path, env: dict, poll_seconds=None):
    """Entry point used by main.py --platform coindcx_futures."""
    import yaml

    from ..healthcheck import ping
    from .coindcx_futures import CoinDCXFutures

    cfg = yaml.safe_load((root / "config" / "coindcx_futures.yaml").read_text())
    mode = env.get("TRADING_MODE", "paper").lower()
    setups = load_setups(cfg)
    client = CoinDCXFutures(env.get("COINDCX_API_KEY", ""), env.get("COINDCX_API_SECRET", ""),
                            cfg.get("margin_currency", "USDT"))
    bot = PriceLevelBot(setups, client, mode, root / "state", root / "logs", cfg.get("fee_pct", 0.05))
    hc = env.get("HC_PING_COINDCX_FUTURES")
    poll = poll_seconds or int(cfg.get("poll_seconds", 5))
    log.info("CoinDCX futures price-level bot starting in %s mode: %s", mode.upper(),
             ", ".join(f"{s.name}({s.side} {s.pair} @{s.entry_price} TP {s.take_profit} SL {s.stop_loss} "
                       f"x{s.leverage})" for s in bot.setups) or "no enabled setups")
    failures, last_ping, last_status = 0, 0.0, 0.0
    while True:
        try:
            prices = bot.tick()
            failures = 0
            now = time.time()
            if now - last_status >= 300:          # a status line every 5 minutes
                log.info("prices %s | %s", prices, {k: v.phase for k, v in bot.state.items()})
                last_status = now
            if now - last_ping >= 60:             # Healthchecks once a minute is plenty
                ping(hc)
                last_ping = now
        except Exception as e:
            failures += 1
            log.exception("loop failed (%d in a row)", failures)
            if failures in (3, 30, 300):
                ping(hc, "fail", repr(e))
        time.sleep(poll)
