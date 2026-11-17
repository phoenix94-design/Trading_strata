"""The two-level timed stop (sl1 / sl2): pure logic, no exchange calls, so it is easy to test and change.

For a LONG (a short is the mirror image):

    price > sl1          SAFE     no timer. One tick back up here stops and resets the timer.
    sl2 < price <= sl1   WARNING  the timer runs (it starts on the first tick at or below sl1).
                                  If price is still in this zone when the timer runs out:
                                  close the position at market.
    price <= sl2         STOP     close the position at sl2.

Reaching sl1 again after a reset starts the full timer again.
"""
from dataclasses import dataclass

SAFE, WARNING, STOP = "safe", "warning", "stop"

# events returned by TimedStop.on_tick / check
TIMER_STARTED = "timer_started"
TIMER_RESET = "timer_reset"
HIT_SL2 = "sl2"
TIMER_EXPIRED = "timer_expired"


@dataclass
class TimedStop:
    side: str               # long | short
    sl1: float
    sl2: float
    timer_seconds: float

    def zone(self, price):
        d = 1 if self.side == "long" else -1
        if d * price <= d * self.sl2:
            return STOP
        if d * price <= d * self.sl1:
            return WARNING
        return SAFE

    def on_tick(self, timer_started_at, price, ts):
        """Feed one trade tick (price, unix seconds). Returns (new_timer_started_at, event or None)."""
        z = self.zone(price)
        if z == STOP:
            return timer_started_at, HIT_SL2
        if z == SAFE:
            return None, (TIMER_RESET if timer_started_at is not None else None)
        if timer_started_at is None:
            return ts, TIMER_STARTED
        if ts - timer_started_at >= self.timer_seconds:
            return timer_started_at, TIMER_EXPIRED
        return timer_started_at, None

    def check(self, timer_started_at, now):
        """Wall-clock check between ticks: has the timer run out?"""
        if timer_started_at is not None and now - timer_started_at >= self.timer_seconds:
            return TIMER_EXPIRED
        return None

    def seconds_left(self, timer_started_at, now):
        if timer_started_at is None:
            return None
        return max(0.0, self.timer_seconds - (now - timer_started_at))
