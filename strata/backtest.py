"""Backtest a strategy on historical candles before it goes anywhere near money.

    venv/bin/python -m strata.backtest --platform coindcx --symbol I-BTC_INR
    venv/bin/python -m strata.backtest --csv data.csv          # columns: ts,open,high,low,close[,volume]

Fills: entries/exits at the next candle's open; stops at the stop price (or the open if it gapped
through). Costs: FEE_PCT per side. Same position sizing as live.
"""
import argparse
import csv
import json

from . import strategies
from .brokers.paper import FEE_PCT
from .models import Candle, Position
from .risk import RiskConfig, position_size


def backtest(candles, strategy, risk=RiskConfig(), start_cash=100000.0, qty_step=1e-8, fee_pct=FEE_PCT):
    cash, pos, trades, curve = start_cash, None, [], []
    pending = None
    for i in range(len(candles)):
        c = candles[i]
        # 1) execute what the previous candle decided, at this candle's open
        if pending and pending.action == "buy" and pos is None:
            qty = position_size(cash, cash, c.open, pending.stop, risk, qty_step, qty_step)
            if qty > 0 and pending.stop < c.open:
                cash -= qty * c.open * (1 + fee_pct / 100)
                pos = Position("bt", qty, c.open, pending.stop, c.ts)
        elif pending and pending.action == "sell" and pos is not None:
            cash += pos.qty * c.open * (1 - fee_pct / 100)
            trades.append((pos.entry, c.open, pos.qty, "signal"))
            pos = None
        pending = None
        # 2) stop inside this candle
        if pos is not None and c.low <= pos.stop:
            fill = min(pos.stop, c.open)
            cash += pos.qty * fill * (1 - fee_pct / 100)
            trades.append((pos.entry, fill, pos.qty, "stop"))
            pos = None
        # 3) decide on the closed candle
        if i + 1 >= strategy.warmup:
            sig = strategy.decide(candles[: i + 1], pos)
            if sig.action in ("buy", "sell"):
                pending = sig
            elif pos is not None and sig.stop and sig.stop > pos.stop:
                pos.stop = sig.stop
        curve.append(cash + (pos.qty * c.close if pos else 0))

    if pos is not None:
        cash += pos.qty * candles[-1].close * (1 - fee_pct / 100)
        trades.append((pos.entry, candles[-1].close, pos.qty, "end"))
    return report(start_cash, cash, trades, curve, candles)


def report(start, end, trades, curve, candles):
    peak, mdd = curve[0] if curve else start, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    wins = [t for t in trades if t[1] > t[0]]
    gross_win = sum((x - e) * q for e, x, q, _ in wins)
    gross_loss = -sum((x - e) * q for e, x, q, _ in trades if x <= e)
    return {
        "candles": len(candles),
        "start_equity": round(start, 2),
        "end_equity": round(end, 2),
        "return_pct": round((end / start - 1) * 100, 2),
        "buy_and_hold_pct": round((candles[-1].close / candles[0].close - 1) * 100, 2) if candles else 0,
        "max_drawdown_pct": round(mdd * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
    }


def load_csv(path):
    with open(path) as f:
        return [Candle(int(float(r["ts"])), float(r["open"]), float(r["high"]), float(r["low"]),
                       float(r["close"]), float(r.get("volume") or 0)) for r in csv.DictReader(f)]


def main():
    from .config import load_settings
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["zerodha", "coindcx"], default="coindcx")
    ap.add_argument("--symbol")
    ap.add_argument("--csv")
    ap.add_argument("--candles", type=int, default=1000)
    a = ap.parse_args()
    s = load_settings(a.platform)
    sc = s.cfg["strategy"]
    strat = strategies.build(sc["name"], sc.get("params"))
    if a.csv:
        candles, step = load_csv(a.csv), 1e-8
    else:
        from . import brokers
        _, data = brokers.build(a.platform, s, s.state_dir)
        sym = a.symbol or s.symbols[0]
        candles, step = data.candles(sym, s.cfg["interval"], a.candles), data.lot(sym)[0]
    print(json.dumps(backtest(candles, strat, s.risk, s.paper_cash, step), indent=2))


if __name__ == "__main__":
    main()
