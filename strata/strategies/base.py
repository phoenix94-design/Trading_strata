from abc import ABC, abstractmethod

from ..models import Candle, Position, Signal


class Strategy(ABC):
    """A strategy looks at closed candles and the open position and returns one Signal.

    It never places orders or sizes positions itself; the engine and risk module do that.
    """

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Closed candles needed before the strategy can decide anything."""

    @abstractmethod
    def decide(self, candles: list[Candle], position: Position | None) -> Signal: ...
