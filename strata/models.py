from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candle:
    ts: int          # candle open time, epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    stop: float
    opened_ts: int = 0
    stop_order_id: str | None = None   # id of the protective stop held at the broker


@dataclass
class Signal:
    action: str                 # "buy", "sell" or "hold"
    stop: float | None = None   # protective stop for a buy
    reason: str = ""
    meta: dict = field(default_factory=dict)
