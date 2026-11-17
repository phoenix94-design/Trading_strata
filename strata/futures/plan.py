"""A trade plan: everything decided before a trade, checked before any order is sent.

One plan = one trade on one CoinDCX futures pair:

    entry       "market", or a limit price (cancelled after entry_expiry_minutes if not filled)
    take_profits one or more {price, percent}; the percents must add up to exactly 100
    sl1, sl2    the two stops; sl1 is nearer the entry, sl2 further away (see stops.py)
    sl1_timer_minutes  how long price may stay between sl1 and sl2 before the trade is closed
    margin      how much margin to put in, in margin_currency (USDT or INR)
    leverage    asked before every trade
"""
import math
from dataclasses import dataclass, field

MAX_LEVERAGE = 100      # the app offers up to 100x on some pairs; each pair's own limit is enforced by CoinDCX
LIQ_SAFETY = 0.8        # sl2 must sit within 80% of the distance to the (estimated) liquidation price
DEFAULT_MMR = 0.005     # maintenance margin used for that estimate when the instrument doesn't say


class PlanError(ValueError):
    """The plan can't be traded as written. The message says why, in plain words."""


@dataclass
class TakeProfit:
    price: float
    percent: float


@dataclass
class Instrument:
    """The exchange's rules for one pair (from /data/instrument)."""
    pair: str
    qty_step: float = 0.001
    min_qty: float = 0.001
    min_notional: float = 0.0
    price_tick: float = 0.01
    maker_fee_pct: float = 0.0236
    taker_fee_pct: float = 0.059
    mmr: float = DEFAULT_MMR
    max_leverage: float = MAX_LEVERAGE

    @classmethod
    def from_api(cls, pair, d):
        tiers = d.get("dynamic_safety_margin_details") or {}
        mmr = min(float(v) for v in tiers.values()) / 100 if tiers else DEFAULT_MMR
        lev_tiers = d.get("dynamic_position_leverage_details") or {}
        if lev_tiers:
            max_lev = max(float(k) for k in lev_tiers)
        else:
            caps = [float(v) for v in (d.get("max_leverage_long"), d.get("max_leverage_short")) if v]
            max_lev = max(caps) if caps else MAX_LEVERAGE
        return cls(
            pair=pair,
            qty_step=float(d.get("quantity_increment") or 0.001),
            min_qty=float(d.get("min_quantity") or d.get("min_trade_size") or d.get("quantity_increment") or 0.001),
            min_notional=float(d.get("min_notional") or 0),
            price_tick=float(d.get("price_increment") or 0.01),
            maker_fee_pct=float(d.get("maker_fee") if d.get("maker_fee") is not None else 0.0236),
            taker_fee_pct=float(d.get("taker_fee") if d.get("taker_fee") is not None else 0.059),
            mmr=mmr,
            max_leverage=max_lev,
        )

    def round_qty(self, qty):
        """Round DOWN to the quantity step (never buy more than the margin allows)."""
        n = math.floor(qty / self.qty_step + 1e-9)
        return round(n * self.qty_step, 10)

    def round_price(self, price):
        n = round(price / self.price_tick)
        return round(n * self.price_tick, 10)

    def fmt_price(self, price):
        decimals = max(0, -int(math.floor(math.log10(self.price_tick)))) if self.price_tick < 1 else 0
        return f"{self.round_price(price):.{decimals}f}"


@dataclass
class TradePlan:
    id: str
    pair: str                       # e.g. B-BTC_USDT
    side: str                       # long | short
    entry: float | str              # "market" or a limit price
    margin: float                   # in margin_currency
    leverage: int
    take_profits: list
    sl1: float
    sl2: float
    sl1_timer_minutes: float
    margin_currency: str = "USDT"   # USDT or INR
    entry_expiry_minutes: float | None = None
    enabled: bool = True            # false = don't start it (an already-open trade is still managed)
    extra: dict = field(default_factory=dict)   # room for future per-trade options

    # ---------- validation ----------
    def __post_init__(self):
        self.id = str(self.id).strip()
        if not self.id:
            raise PlanError("every trade needs an id")
        self.side = str(self.side).lower().strip()
        if self.side not in ("long", "short"):
            raise PlanError(f"{self.id}: side must be long or short")
        self.margin_currency = str(self.margin_currency).upper().strip()
        if self.margin_currency not in ("USDT", "INR"):
            raise PlanError(f"{self.id}: margin_currency must be USDT or INR")
        self.margin, self.sl1, self.sl2 = float(self.margin), float(self.sl1), float(self.sl2)
        self.sl1_timer_minutes = float(self.sl1_timer_minutes)
        if self.margin <= 0:
            raise PlanError(f"{self.id}: margin must be more than 0")
        if int(self.leverage) != self.leverage or not 1 <= int(self.leverage) <= MAX_LEVERAGE:
            raise PlanError(f"{self.id}: leverage must be a whole number from 1 to {MAX_LEVERAGE}")
        self.leverage = int(self.leverage)
        if self.sl1_timer_minutes <= 0:
            raise PlanError(f"{self.id}: sl1_timer_minutes must be more than 0")

        self.take_profits = [tp if isinstance(tp, TakeProfit) else TakeProfit(float(tp["price"]), float(tp["percent"]))
                             for tp in (self.take_profits or [])]
        if not self.take_profits:
            raise PlanError(f"{self.id}: at least one take profit is needed")
        if any(tp.percent <= 0 for tp in self.take_profits):
            raise PlanError(f"{self.id}: every take profit needs a percent above 0")
        total = sum(tp.percent for tp in self.take_profits)
        if abs(total - 100) > 1e-6:
            raise PlanError(f"{self.id}: take profit percents add up to {total:g}%, they must add up to exactly 100%")

        if self.is_market:
            self.entry = "market"
        else:
            self.entry = float(self.entry)
            if self.entry <= 0:
                raise PlanError(f"{self.id}: entry must be 'market' or a price above 0")
            if not self.entry_expiry_minutes or float(self.entry_expiry_minutes) <= 0:
                raise PlanError(f"{self.id}: a limit entry needs entry_expiry_minutes (cancel if not filled by then)")
            self.entry_expiry_minutes = float(self.entry_expiry_minutes)

        # sl2 beyond sl1, take profits in order, all on the right side
        d = self.direction
        if not d * self.sl2 < d * self.sl1:
            raise PlanError(f"{self.id}: sl2 must be further from the entry than sl1 "
                            f"({'below' if self.is_long else 'above'} it for a {self.side})")
        prices = [tp.price for tp in self.take_profits]
        if any(not d * a < d * b for a, b in zip(prices, prices[1:])):
            raise PlanError(f"{self.id}: take profits must be in order, nearest first")
        if not d * self.sl1 < d * prices[0]:
            raise PlanError(f"{self.id}: sl1 and the first take profit are on the wrong sides for a {self.side}")
        if not self.is_market:
            problem = self.entry_problem(self.entry)
            if problem:
                raise PlanError(f"{self.id}: {problem}")

    # ---------- helpers ----------
    @property
    def is_market(self):
        return isinstance(self.entry, str) and self.entry.strip().lower() == "market"

    @property
    def is_long(self):
        return self.side == "long"

    @property
    def direction(self):            # +1 long, -1 short: "d * a < d * b" means "a is worse than b"
        return 1 if self.is_long else -1

    @property
    def open_side(self):
        return "buy" if self.is_long else "sell"

    @property
    def close_side(self):
        return "sell" if self.is_long else "buy"

    def reached(self, price, level):
        """True when price has reached a take-profit level (at or beyond it, in the profit direction)."""
        return self.direction * price >= self.direction * level

    def entry_problem(self, entry_price, mmr=DEFAULT_MMR):
        """Why entering at entry_price would be wrong, or None."""
        d = self.direction
        if not d * self.sl1 < d * entry_price < d * self.take_profits[0].price:
            return (f"entry {entry_price:g} must be between sl1 {self.sl1:g} and the first take profit "
                    f"{self.take_profits[0].price:g}")
        # Isolated margin: liquidation comes after roughly (1/leverage - maintenance margin) of adverse move.
        # sl2 must fire well before that, or the exchange liquidates the trade first.
        liq_move = 1 / self.leverage - mmr
        sl2_move = abs(entry_price - self.sl2) / entry_price
        if liq_move <= 0 or sl2_move >= LIQ_SAFETY * liq_move:
            return (f"sl2 is {sl2_move:.2%} from the entry, but at {self.leverage}x the liquidation price is only "
                    f"about {max(liq_move, 0):.2%} away; lower the leverage or move sl2 closer")
        return None

    def margin_in_usdt(self, inr_per_usdt):
        return self.margin if self.margin_currency == "USDT" else self.margin / inr_per_usdt

    def quantity(self, price, inst: Instrument, inr_per_usdt=None):
        """Position size from margin x leverage, rounded down to the pair's quantity step."""
        if self.margin_currency == "INR" and not inr_per_usdt:
            raise PlanError(f"{self.id}: need the INR/USDT rate to size an INR-margin trade")
        qty = inst.round_qty(self.margin_in_usdt(inr_per_usdt) * self.leverage / price)
        if qty < inst.min_qty:
            raise PlanError(f"{self.id}: margin {self.margin:g} {self.margin_currency} x{self.leverage} buys {qty:g}, "
                            f"below the minimum {inst.min_qty:g} for {self.pair}")
        if qty * price < inst.min_notional:
            raise PlanError(f"{self.id}: position value {qty * price:.2f} USDT is below the minimum "
                            f"{inst.min_notional:g} USDT for {self.pair}")
        return qty

    def split(self, qty, inst: Instrument, strict=True):
        """Quantity for each take profit. The last one gets whatever is left, so they always add up to qty.

        strict=True (before ordering): refuse if any part is below the pair's minimum.
        strict=False (after a partial fill): fold too-small parts into the next take profit instead.
        """
        parts = [inst.round_qty(qty * tp.percent / 100) for tp in self.take_profits[:-1]]
        parts.append(round(qty - sum(parts), 10))
        if all(p >= inst.min_qty for p in parts):
            return parts
        if strict:
            raise PlanError(f"{self.id}: {qty:g} can't be split {'/'.join(f'{tp.percent:g}%' for tp in self.take_profits)} "
                            f"because each part must be at least {inst.min_qty:g}; add margin or use fewer take profits")
        merged, carry = [], 0.0
        for p in parts:
            p = round(p + carry, 10)
            if p >= inst.min_qty:
                merged.append(p)
                carry = 0.0
            else:
                merged.append(0.0)
                carry = p
        if carry:
            last = max(i for i, p in enumerate(merged) if p > 0) if any(merged) else len(merged) - 1
            merged[last] = round(merged[last] + carry, 10)
        return merged


def load_plans(raw_trades, log=None):
    """Build plans from config. A bad trade is reported and skipped; it never stops the others."""
    plans, errors, seen = [], {}, set()
    for raw in raw_trades or []:
        tid = str((raw or {}).get("id", "")).strip() or "<no id>"
        try:
            if tid in seen:
                raise PlanError(f"{tid}: duplicate trade id")
            known = set(TradePlan.__dataclass_fields__)
            extra = {k: v for k, v in raw.items() if k not in known}
            plan = TradePlan(**{k: v for k, v in raw.items() if k in known}, extra=extra)
            plans.append(plan)
        except (PlanError, TypeError, ValueError, KeyError) as e:
            errors[tid] = str(e)
            if log:
                log.error("trade %s rejected: %s", tid, e)
        seen.add(tid)
    return plans, errors
