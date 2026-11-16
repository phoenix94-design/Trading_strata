import math
from pathlib import Path

from strata.backtest import backtest
from strata.config import Settings
from strata.engine import Engine
from strata.brokers.paper import PaperBroker
from strata.brokers.base import MarketData
from strata.indicators import atr, ema
from strata.models import Candle
from strata.risk import RiskConfig, daily_loss_hit, position_size
from strata.strategies.ema_trend import EmaTrend


def wave(n=900, start=100.0):
    """Trending sine wave: gives the EMA strategy real crosses in both directions."""
    out, p = [], start
    for i in range(n):
        o = p
        p = start * (1 + 0.0015 * i) * (1 + 0.08 * math.sin(i / 40))
        out.append(Candle(i * 3600, o, max(o, p) * 1.002, min(o, p) * 0.998, p, 1))
    return out


def test_ema_matches_sma_seed_and_aligns():
    e = ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2 and len(e) == 5


def test_atr_positive():
    a = atr(wave(50), 14)
    assert len(a) == 50 and all(x > 0 for x in a[13:])


def test_position_size_respects_risk_and_caps():
    cfg = RiskConfig(risk_per_trade_pct=1, max_position_pct=100)
    assert position_size(100000, 100000, 100, 95, cfg) == 200          # 1000 risk / 5 per unit
    cfg = RiskConfig(risk_per_trade_pct=1, max_position_pct=10)
    assert position_size(100000, 100000, 100, 95, cfg) == 100          # capped at 10% of equity
    assert position_size(100000, 100000, 100, 101, cfg) == 0           # stop above entry
    assert position_size(1000, 1000, 5000, 4900, cfg, qty_step=1) == 0  # too small for one share


def test_daily_loss():
    assert daily_loss_hit(100, 96, RiskConfig(max_daily_loss_pct=3))
    assert not daily_loss_hit(100, 98, RiskConfig(max_daily_loss_pct=3))


def test_backtest_runs_and_trades():
    r = backtest(wave(), EmaTrend())
    assert r["trades"] > 0 and r["max_drawdown_pct"] < 50


class FakeData(MarketData):
    def __init__(self, candles):
        self.all, self.n = candles, 260

    def candles(self, symbol, interval, limit):
        return self.all[: self.n][-limit:]

    def last_price(self, symbol):
        return self.all[self.n - 1].close

    def lot(self, symbol):
        return 1e-6, 1e-6


def test_engine_paper_end_to_end(tmp_path: Path):
    s = Settings("coindcx", "paper", {"strategy": {"name": "ema_trend"}, "interval": "1h"}, {},
                 RiskConfig(), tmp_path, tmp_path, 100000, ["X"])
    data = FakeData(wave())
    broker = PaperBroker("coindcx", data, tmp_path, 100000)
    eng = Engine(s, broker, data)
    for n in range(260, 900):
        data.n = n
        eng.tick()
    trades = (tmp_path / "trades_coindcx.csv").read_text().splitlines()
    assert len(trades) > 2                                # header + buys/sells happened
    # restart safety: a fresh engine on the same state does not re-trade the same candle
    eng2 = Engine(s, PaperBroker("coindcx", data, tmp_path, 100000), data)
    before = len(trades)
    eng2.tick()
    assert len((tmp_path / "trades_coindcx.csv").read_text().splitlines()) == before


def test_kill_switch_blocks_entries(tmp_path: Path):
    (tmp_path / "KILL").touch()
    s = Settings("coindcx", "paper", {"strategy": {"name": "ema_trend"}, "interval": "1h"}, {},
                 RiskConfig(), tmp_path, tmp_path, 100000, ["X"])
    data = FakeData(wave())
    eng = Engine(s, PaperBroker("coindcx", data, tmp_path, 100000), data)
    for n in range(260, 900):
        data.n = n
        eng.tick()
    assert not (tmp_path / "trades_coindcx.csv").exists()
