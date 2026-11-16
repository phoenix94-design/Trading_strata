from abc import ABC, abstractmethod

from ..models import Candle, Position


class MarketData(ABC):
    @abstractmethod
    def candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        """Closed candles only, oldest first."""

    @abstractmethod
    def last_price(self, symbol: str) -> float: ...

    def lot(self, symbol: str) -> tuple[float, float]:
        """(qty_step, min_qty) for a symbol."""
        return 1.0, 1.0


class Broker(ABC):
    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def buy(self, symbol: str, qty: float, stop: float) -> Position:
        """Market buy, then place the protective stop at the broker where possible."""

    @abstractmethod
    def sell(self, symbol: str, reason: str) -> float:
        """Close the whole position at market (cancelling its stop). Returns exit price."""

    @abstractmethod
    def move_stop(self, symbol: str, new_stop: float) -> None: ...

    def check_stops(self) -> list[tuple[str, float]]:
        """Brokers that hold stops themselves return []. Paper trading simulates them here."""
        return []

    def sync(self) -> list[tuple[str, float]]:
        """Reconcile with the broker; returns positions that were closed outside Strata."""
        return []
