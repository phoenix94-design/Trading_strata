from .ema_trend import EmaTrend

STRATEGIES = {"ema_trend": EmaTrend}


def build(name, params):
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; known: {sorted(STRATEGIES)}")
    return STRATEGIES[name](**(params or {}))
