"""Plain-Python indicators (no pandas, so the server install stays tiny)."""


def ema(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    # pad the front so out[i] lines up with values[i]
    return [None] * (period - 1) + out


def atr(candles, period=14):
    if len(candles) <= period:
        return []
    trs = [candles[0].high - candles[0].low]
    for prev, c in zip(candles, candles[1:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    out = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)   # Wilder smoothing
    return [None] * (period - 1) + out
