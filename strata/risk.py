"""Position sizing and the safety switches that sit above every strategy."""
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0      # lose at most this % of equity if the stop is hit
    max_position_pct: float = 20.0       # never put more than this % of equity in one symbol
    max_daily_loss_pct: float = 3.0      # stop opening trades for the day after this drawdown
    max_open_positions: int = 3


def position_size(equity, cash, entry, stop, cfg: RiskConfig, qty_step=1.0, min_qty=0.0):
    """Quantity to buy so that hitting the stop costs about risk_per_trade_pct of equity."""
    per_unit_risk = entry - stop
    if entry <= 0 or per_unit_risk <= 0:
        return 0.0
    by_risk = equity * cfg.risk_per_trade_pct / 100 / per_unit_risk
    by_cap = equity * cfg.max_position_pct / 100 / entry
    by_cash = cash * 0.995 / entry          # leave room for fees
    qty = min(by_risk, by_cap, by_cash)
    qty = math.floor(qty / qty_step) * qty_step
    qty = round(qty, 10)
    return qty if qty >= max(min_qty, qty_step) else 0.0


def kill_switch_on(state_dir: Path) -> bool:
    """`touch state/KILL` on the server stops all new entries without stopping the service."""
    return (state_dir / "KILL").exists()


def daily_loss_hit(day_start_equity, equity, cfg: RiskConfig) -> bool:
    if day_start_equity <= 0:
        return False
    return (day_start_equity - equity) / day_start_equity * 100 >= cfg.max_daily_loss_pct
