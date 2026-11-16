from ..indicators import atr, ema
from ..models import Signal
from .base import Strategy


class EmaTrend(Strategy):
    """Long-only trend follower.

    Buy  : fast EMA crosses above slow EMA and price is above the trend EMA.
    Stop : entry - atr_mult * ATR, then trailed up (never down) as price rises.
    Sell : fast EMA crosses back below slow EMA (the stop handles everything else).
    """

    def __init__(self, fast=20, slow=50, trend=200, atr_period=14, atr_mult=2.5):
        if not fast < slow <= trend:
            raise ValueError("need fast < slow <= trend")
        self.fast, self.slow, self.trend = fast, slow, trend
        self.atr_period, self.atr_mult = atr_period, atr_mult

    @property
    def warmup(self):
        return self.trend + 2

    def decide(self, candles, position):
        if len(candles) < self.warmup:
            return Signal("hold", reason=f"warming up ({len(candles)}/{self.warmup} candles)")
        closes = [c.close for c in candles]
        f, s, t = ema(closes, self.fast), ema(closes, self.slow), ema(closes, self.trend)
        a = atr(candles, self.atr_period)[-1]
        price = closes[-1]
        crossed_up = f[-2] <= s[-2] and f[-1] > s[-1]
        crossed_down = f[-2] >= s[-2] and f[-1] < s[-1]
        meta = {"fast": f[-1], "slow": s[-1], "trend": t[-1], "atr": a}

        if position is None:
            if crossed_up and price > t[-1]:
                return Signal("buy", stop=price - self.atr_mult * a, reason="EMA cross up in uptrend", meta=meta)
            return Signal("hold", reason="no entry", meta=meta)

        if crossed_down:
            return Signal("sell", reason="EMA cross down", meta=meta)
        trailed = price - self.atr_mult * a
        if trailed > position.stop:
            return Signal("hold", stop=trailed, reason="trail stop up", meta=meta)
        return Signal("hold", reason="in trade", meta=meta)
