"""Tests for the timed-stop strategy: plan checks, the sl1/sl2 timer, take profits, entries, restarts."""
import csv

import pytest

from strata.futures.executors import PaperExecutor
from strata.futures.plan import Instrument, PlanError, TradePlan, load_plans
from strata.futures.stops import HIT_SL2, TIMER_EXPIRED, TIMER_RESET, TIMER_STARTED, TimedStop
from strata.futures.trader import TradeManager

INSTRUMENT = {"quantity_increment": 0.001, "min_quantity": 0.001, "min_notional": 6, "price_increment": 0.1,
              "maker_fee": 0.0236, "taker_fee": 0.059, "dynamic_position_leverage_details": {"5": 1, "100": 1},
              "dynamic_safety_margin_details": {"20000": 0.5}}


def trade(**kw):
    base = dict(id="t1", pair="B-BTC_USDT", side="long", entry="market", margin=100, leverage=5,
                take_profits=[{"price": 65000, "percent": 50}, {"price": 66000, "percent": 50}],
                sl1=63000, sl2=62500, sl1_timer_minutes=10)
    base.update(kw)
    return base


def short_trade(**kw):
    return trade(side="short", take_profits=[{"price": 63000, "percent": 50}, {"price": 62000, "percent": 50}],
                 sl1=65000, sl2=65500, **kw)


class FakeClient:
    """Stands in for CoinDCX's public endpoints."""

    def __init__(self):
        self.feed = {}

    def trades(self, pair):
        return [{"timestamp": t, "price": p, "quantity": 1} for t, p in self.feed.get(pair, [])[-30:]]

    def instrument(self, pair, margin_currency=None):
        return INSTRUMENT

    def spot_inr_per_usdt(self):
        return 100.0

    def last_price(self, pair):
        return self.feed[pair][-1][1]


class FakeLive(PaperExecutor):
    """Paper fills, but behaves like the exchange for protection and position checks."""
    mode = "live"

    def __init__(self, client, fail_protect=False):
        super().__init__(client, 100.0)
        self.fail_protect, self.exchange_qty, self.calls = fail_protect, None, []

    def protect(self, plan, st):
        self.calls.append(("protect", st.sl2, plan.take_profits[-1].price))
        if self.fail_protect:
            raise RuntimeError("rejected by exchange")

    def position(self, plan, st):
        return (st.qty if self.exchange_qty is None else self.exchange_qty), st.entry_price, None

    def close(self, plan, st, price_hint):
        self.calls.append(("close",))
        return price_hint


class Sim:
    def __init__(self, tmp_path, trades, executor=None):
        self.tmp, self.trades_cfg = tmp_path, trades
        self.client = FakeClient()
        self.ex = executor or PaperExecutor(self.client, 100.0)
        self.ex.client = self.client
        self.t = 1_000_000.0
        self.mgr = self._manager()

    def _manager(self):
        plans, rejected = load_plans(self.trades_cfg)
        return TradeManager(plans, self.ex, self.tmp, self.tmp, rejected)

    def restart(self, trades=None):
        if trades is not None:
            self.trades_cfg = trades
        self.mgr = self._manager()

    def price(self, p, pair="B-BTC_USDT", dt=1.0):
        self.t += dt
        self.client.feed.setdefault(pair, []).append((int(self.t * 1000), p))
        self.mgr.tick(now=self.t)

    def wait(self, seconds):
        self.t += seconds
        self.mgr.tick(now=self.t)

    def st(self, tid="t1"):
        return self.mgr.state[tid]

    def rows(self):
        path = self.tmp / "trades_coindcx_trades.csv"
        return list(csv.DictReader(path.open())) if path.exists() else []


# ---------------- plan checks ----------------
def test_plan_validation():
    TradePlan(**trade())
    TradePlan(**short_trade())
    bad = [
        trade(take_profits=[{"price": 65000, "percent": 50}, {"price": 66000, "percent": 40}]),  # 90%
        trade(sl2=63500),                                    # sl2 nearer than sl1
        trade(take_profits=[{"price": 66000, "percent": 50}, {"price": 65000, "percent": 50}]),  # out of order
        trade(entry=63800),                                  # limit without expiry
        trade(leverage=0),
        trade(margin_currency="EUR"),
        trade(side="sideways"),
        trade(take_profits=[]),
        trade(entry=64000, entry_expiry_minutes=30, leverage=50),   # sl2 beyond liquidation at 50x
    ]
    for b in bad:
        with pytest.raises(PlanError):
            TradePlan(**b)


def test_sizing_and_split():
    inst = Instrument.from_api("B-BTC_USDT", INSTRUMENT)
    plan = TradePlan(**trade())
    qty = plan.quantity(64000, inst)
    assert qty == 0.007                                     # 100 USDT x5 / 64000, rounded down
    assert plan.split(qty, inst) == [0.003, 0.004]          # last part takes the remainder
    inr = TradePlan(**trade(margin=10000, margin_currency="INR"))
    assert inr.quantity(64000, inst, inr_per_usdt=100) == 0.007
    small = TradePlan(**trade(margin=20))
    assert small.quantity(64000, inst) == 0.001
    with pytest.raises(PlanError):
        small.split(0.001, inst)                            # can't split 0.001 into two parts
    assert small.split(0.001, inst, strict=False) == [0.0, 0.001]
    assert inst.max_leverage == 100


# ---------------- the timed stop on its own ----------------
def test_timed_stop_rules():
    s = TimedStop("long", sl1=100, sl2=95, timer_seconds=60)
    t, ev = s.on_tick(None, 101, 0)
    assert (t, ev) == (None, None)
    t, ev = s.on_tick(None, 100, 10)              # touching sl1 counts
    assert (t, ev) == (10, TIMER_STARTED)
    t, ev = s.on_tick(t, 98, 30)
    assert (t, ev) == (10, None)
    t, ev = s.on_tick(t, 100.1, 40)               # one tick above sl1 resets
    assert (t, ev) == (None, TIMER_RESET)
    t, ev = s.on_tick(t, 99, 50)                  # full timer again
    assert (t, ev) == (50, TIMER_STARTED)
    assert s.check(t, 109) is None and s.check(t, 110) == TIMER_EXPIRED
    assert s.on_tick(t, 99, 111)[1] == TIMER_EXPIRED
    assert s.on_tick(t, 95, 60)[1] == HIT_SL2     # touching sl2 counts


# ---------------- the user's three stop scenarios ----------------
def test_scenario_1_sl2_while_timer_running(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.price(64000)
    assert sim.st().phase == "open" and sim.st().qty == 0.007
    sim.price(63000)
    assert sim.st().timer_started_at is not None
    sim.price(62800, dt=60)
    sim.price(62500, dt=60)                                  # sl2 reached, timer still running
    st = sim.st()
    assert st.phase == "closed" and "sl2" in st.note
    close = sim.rows()[-1]
    assert close["event"] == "close" and float(close["price"]) == 62500
    assert st.realized_pnl == pytest.approx(-1500 * 0.007 - 0.007 * (64000 + 62500) * 0.059 / 100)


def test_scenario_2_back_above_sl1_resets_and_repeats(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.price(64000)
    sim.price(62900)                                         # timer starts
    first = sim.st().timer_started_at
    sim.price(62950, dt=300)                                 # 5 min in the zone
    sim.price(63100, dt=60)                                  # one tick back above sl1: timer stops and resets
    assert sim.st().timer_started_at is None
    sim.price(62900, dt=60)                                  # sl1 again: full timer from now
    second = sim.st().timer_started_at
    assert second > first
    sim.wait(9 * 60)                                         # 9 min after the restart: still open
    assert sim.st().phase == "open"
    sim.wait(61)                                             # 10 min after the restart
    assert sim.st().phase == "closed" and "timer" in sim.st().note


def test_scenario_3_timer_runs_out_in_the_zone(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.price(64000)
    sim.price(62900)
    sim.price(62700, dt=120)
    sim.wait(9 * 60)                                         # 11 min since sl1, no tick back above
    st = sim.st()
    assert st.phase == "closed" and "timer" in st.note
    assert float(sim.rows()[-1]["price"]) == 62700           # closed at market (the last price)


# ---------------- take profits ----------------
def test_partial_then_final_take_profit(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.price(64000)
    sim.price(65000)
    st = sim.st()
    assert st.phase == "open" and st.qty == 0.004 and st.tp_done == [True, False]
    assert sim.rows()[-1]["event"] == "tp1" and float(sim.rows()[-1]["qty"]) == 0.003
    sim.price(64500)
    sim.price(66000)
    st = sim.st()
    assert st.phase == "closed" and st.realized_pnl > 0
    assert [r["event"] for r in sim.rows()] == ["entry", "tp1", "close"]


def test_jump_past_last_take_profit_closes_everything(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.price(64000)
    sim.price(66500)
    assert sim.st().phase == "closed" and sim.st().tp_done == [True, True]
    assert [r["event"] for r in sim.rows()] == ["entry", "close"]


def test_three_take_profits(tmp_path):
    sim = Sim(tmp_path, [trade(margin=200, take_profits=[{"price": 65000, "percent": 50},
                                                          {"price": 65500, "percent": 30},
                                                          {"price": 66000, "percent": 20}])])
    sim.price(64000)                                         # 200 x5 / 64000 = 0.015
    assert sim.st().tp_qty == [0.007, 0.004, 0.004]
    sim.price(65100)
    sim.price(65600)
    assert sim.st().qty == pytest.approx(0.004)
    sim.price(66000)
    assert sim.st().phase == "closed"


# ---------------- short ----------------
def test_short_is_the_mirror_image(tmp_path):
    sim = Sim(tmp_path, [short_trade()])
    sim.price(64000)
    sim.price(65000)                                         # sl1 for a short is above
    assert sim.st().timer_started_at is not None
    sim.price(64900)                                         # back below sl1: reset
    assert sim.st().timer_started_at is None
    sim.price(63000)                                         # tp1 (below)
    assert sim.st().tp_done == [True, False]
    sim.price(65600)                                         # sl2 (above)
    assert sim.st().phase == "closed" and "sl2" in sim.st().note


# ---------------- entries ----------------
def test_market_entry_refused_if_price_already_past_sl1(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.price(62900)
    assert sim.st().phase == "rejected" and sim.rows() == []


def test_limit_entry_fills_or_expires(tmp_path):
    sim = Sim(tmp_path, [trade(entry=63800, entry_expiry_minutes=30),
                         trade(id="t2", pair="B-ETH_USDT", entry=1700, entry_expiry_minutes=30,
                               take_profits=[{"price": 1800, "percent": 100}], sl1=1650, sl2=1620, margin=50)])
    sim.price(1750, pair="B-ETH_USDT", dt=0)
    sim.price(64000)
    assert sim.st().phase == "entry_working" and sim.st("t2").phase == "entry_working"
    sim.price(63850)
    assert sim.st().phase == "entry_working"
    sim.price(63790)                                         # traded through the limit
    assert sim.st().phase == "open" and sim.st().entry_price == 63800
    sim.wait(31 * 60)
    assert sim.st("t2").phase == "cancelled" and "not filled" in sim.st("t2").note


def test_several_trades_and_one_per_pair(tmp_path):
    sim = Sim(tmp_path, [trade(),
                         trade(id="eth", pair="B-ETH_USDT", take_profits=[{"price": 1900, "percent": 100}],
                               sl1=1700, sl2=1680, margin=50),
                         trade(id="t3")])                     # second BTC trade while t1 runs
    sim.price(1750, pair="B-ETH_USDT", dt=0)
    sim.price(64000)
    assert sim.st("t1").phase == "open" and sim.st("eth").phase == "open"
    assert sim.st("t3").phase == "rejected" and "already has an active trade" in sim.st("t3").note


# ---------------- robustness ----------------
def test_restart_keeps_the_timer(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.price(64000)
    sim.price(62900)
    started = sim.st().timer_started_at
    sim.restart()
    assert sim.st().timer_started_at == started and sim.st().phase == "open"
    sim.wait(10 * 60)
    assert sim.st().phase == "closed"


def test_running_trade_is_frozen_against_config_edits(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.price(64000)
    sim.restart([trade(sl1=63500, sl2=63200)])               # edited after it started
    assert sim.st().sl1 == 63000 and sim.mgr.plans["t1"].sl2 == 62500


def test_bad_trade_does_not_stop_the_others(tmp_path):
    bad = trade(id="bad", take_profits=[{"price": 65000, "percent": 60}])
    sim = Sim(tmp_path, [bad, trade()])
    sim.price(64000)
    assert sim.st("bad").phase == "invalid" and "100" in sim.st("bad").note
    assert sim.st("t1").phase == "open"
    sim.restart([trade(id="bad", pair="B-ETH_USDT", take_profits=[{"price": 1900, "percent": 100}],
                       sl1=1700, sl2=1680, margin=50), trade()])     # fixed under the same id
    assert sim.st("bad").phase == "new"


def test_disabled_trade_waits(tmp_path):
    sim = Sim(tmp_path, [trade(enabled=False)])
    sim.price(64000)
    assert sim.st().phase == "new"
    sim.restart([trade(enabled=True)])
    sim.price(64000)
    assert sim.st().phase == "open"


def test_interrupted_entry_is_never_resent(tmp_path):
    sim = Sim(tmp_path, [trade()])
    sim.mgr.state["t1"].phase = "entering"
    sim.mgr.state["t1"].plan = dict(TradePlan(**trade()).__dict__,
                                    take_profits=trade()["take_profits"])
    sim.mgr.save()
    sim.restart()
    sim.price(64000)
    assert sim.st().phase == "failed" and sim.rows() == []


# ---------------- live-mode safety (fake exchange) ----------------
def test_live_places_sl2_and_last_tp_on_exchange(tmp_path):
    ex = FakeLive(FakeClient())
    sim = Sim(tmp_path, [trade()], executor=ex)
    sim.price(64000)
    assert ("protect", 62500, 66000) in ex.calls and sim.st().protected


def test_live_closes_if_stop_cannot_be_placed(tmp_path):
    ex = FakeLive(FakeClient(), fail_protect=True)
    sim = Sim(tmp_path, [trade()], executor=ex)
    sim.price(64000)
    st = sim.st()
    assert st.phase == "closed" and "could not place stop" in st.note and ("close",) in ex.calls


def test_live_notices_exchange_stop_fired(tmp_path):
    ex = FakeLive(FakeClient())
    sim = Sim(tmp_path, [trade()], executor=ex)
    sim.price(64000)
    ex.exchange_qty = 0.0                                    # CoinDCX's sl2 closed it while we were away
    sim.price(62400)
    st = sim.st()
    assert st.phase == "closed" and "sl2 fired on CoinDCX" in st.note
    assert float(sim.rows()[-1]["price"]) == 62500
