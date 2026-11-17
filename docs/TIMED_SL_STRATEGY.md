# Timed-stop strategy (CoinDCX futures)

Ishwar's main strategy, agreed 2026-11-17. Service: `strata@coindcx_trades`. Trades are set in
`config/trades.yaml` on the server. That file is not in git; `install.sh` creates it from
`config/trades.example.yaml`. This file is the handoff: the rules, how to add a trade, how it runs, and
how to change it.

## The rules

| Part | Rule |
|---|---|
| Instruments | CoinDCX futures pairs (`B-XXX_USDT`): crypto plus gold (`B-XAU_USDT`), silver (`B-XAG_USDT`), oil (`B-CL_USDT`, `B-BZ_USDT`), gas, copper. Margin in **USDT** or **INR** (the app's "INR-M Futures"). **The app's "Global Futures" (S&P500, NSDQ100, NVDA, pre-IPO...) are not in CoinDCX's API and can't be automated.** |
| Direction | Long or short. |
| Entry | `market`, or a limit price that is cancelled if it isn't filled within `entry_expiry_minutes`. If only part fills before the expiry, Strata trades that part. |
| Take profits | One or more. Each closes a set **percent** of the position, and the percents must add up to **exactly 100**. The last one closes whatever is left. |
| sl1 / sl2 | Two stops. sl1 is nearer the entry and sl2 is further away. See the next section. |
| Margin, leverage | Asked before every trade. Margin is given in USDT or ₹. |
| Several trades | Yes, side by side. Only one active trade per pair, because CoinDCX keeps one position per pair. |
| Later | Moving the stops after a take profit (hook ready, see "Extending"). Telegram alerts. |

### The two stops (long; a short is the mirror image)

```
          take profits ...
  -------- entry ---------
     SAFE: no timer. One tick here stops AND resets the timer.
  -------- sl1 -----------   price touches sl1 -> timer starts (full length)
     WARNING: timer running
       - timer runs out while price is still here -> close at market   (scenario 3)
       - one tick back above sl1 -> timer stops and resets            (scenario 2)
         (touching sl1 again starts the full timer again)
  -------- sl2 -----------   price touches sl2 -> close at sl2         (scenario 1)
```

### Worked example (confirmed with Ishwar)

Long BTC: limit 63,500; TP1 64,500 (50%), TP2 65,500 (50%); sl1 63,000; sl2 62,500; timer 10 min.
1. The limit fills at 63,500. Straight away, a whole-position stop at 62,500 and a whole-position
   take profit at 65,500 are placed **on CoinDCX**.
2. 10:20 price touches 63,000, so the timer starts. 10:24 price is at 63,200, so the timer resets.
   10:31 price is at 62,950, so the full 10-minute timer starts again.
3. Then one of: 62,500 is reached, so the trade closes at sl2. Or price stays between 62,500 and
   63,000 until 10:41, so it closes at market. Or price climbs back above 63,000, so the timer
   resets; TP1 then sells half and TP2 the rest.

## Adding a trade

Before each trade, ask Ishwar for: pair, margin currency, side, entry (market or limit, plus the
expiry for a limit), take profits with percents, sl1, sl2, timer minutes, **margin and leverage**.
Then append it to `config/trades.yaml` with a **new id** and restart:

```yaml
  - id: btc-2026-11-18-1
    pair: B-BTC_USDT
    margin_currency: USDT        # or INR
    side: long
    entry: 63500                 # or: market
    entry_expiry_minutes: 120    # limit only
    margin: 50                   # USDT (or ₹ if margin_currency: INR)
    leverage: 5
    take_profits:
      - {price: 64500, percent: 50}
      - {price: 65500, percent: 50}
    sl1: 63000
    sl2: 62500
    sl1_timer_minutes: 10
```
```bash
sudo systemctl restart strata@coindcx_trades     # safe at any time: state and timers persist
journalctl -u strata@coindcx_trades -n 30
```

Strata checks the trade before sending anything, and the log says why if it refuses:
- percents add up to 100
- prices are in the right order for the side
- a limit entry has an expiry
- the size is at least the pair's minimum and every take profit part is too (e.g. BTC: 0.001)
- sl2 is well inside the liquidation distance for the leverage
- for a market entry, the current price is between sl1 and TP1
- the pair is not already in use

**Size:** quantity = margin × leverage ÷ entry price (₹ margin is converted at CoinDCX's own INR
rate), rounded **down** to the pair's step. Example: 50 USDT × 5 at 63,500 = 0.003 BTC.

**Fees (per side):** BTC 0.059% for market orders and 0.0236% for limit orders. Gold, silver and oil
are 0.0118%. A market round trip on BTC costs ~0.12%, so take profits very close to the entry can't
make money.

## What runs where

| Piece | Where | Works when the home server is off? |
|---|---|---|
| sl2 | CoinDCX, whole-position `stop_market` (create_tpsl) | **yes** |
| Last take profit | CoinDCX, whole-position `take_profit_market` | **yes** |
| Earlier take profits | Strata, market orders after a position check | no (resumes on restart) |
| sl1 timer | Strata; timer saved to disk | no (if the timer ran out while offline, it closes right after restart) |
| Limit-entry expiry | Strata | no (cancels on restart) |
| Placing sl2 after a limit fill | Strata, right after the fill | **no**: a limit that fills while the server is off has no sl2 until Strata is back (it places it on its first loop). The UPS and auto-restart keep this window short. |

Why: CoinDCX allows one take profit and one stop per position, and has no reduce-only flag. So
partial take profits can't safely sit on the exchange: after a stop they could open a reverse
position. After every close, Strata cancels any leftover orders for the position.

## States (`state/trades_state.json`)

`new` → `entering` → `entry_working` (limit) → `open` → `closed`, or `rejected` / `cancelled` /
`failed` / `invalid`, with a `note` saying why.
- A trade that has started is **frozen**: its plan is copied into the state, and later edits to the
  config are ignored.
- `entering` is saved *before* the entry order is sent. If Strata crashes there, it checks CoinDCX
  on restart and **never resends** the entry.
- `invalid` = the config entry is wrong. Fixing it under the same id lets it run. `rejected` is
  never retried by itself.
- Trade log: `logs/trades_coindcx_trades.csv` (entry, tp1..n, close, with P&L and fees in USDT).
- A status line goes to the journal every 5 min, including the timer's seconds left.

## Going live (only when Ishwar says so)

1. CoinDCX API key with futures permission; put it in `.env` (`chmod 600`). The home server's
   clock must be NTP-synced, because CoinDCX rejects orders whose timestamp is more than 10 s off.
2. First live trade: the smallest size, low leverage, a pair with nothing open, and watch it:
   - [ ] leverage update accepted (`update_leverage`)
   - [ ] entry fills; the position shows on CoinDCX
   - [ ] the app shows a full-position SL at sl2 and TP at the last take profit
   - [ ] a partial take profit reduces the position (no reverse position)
   - [ ] the close leaves no open orders behind
   - [ ] (to close the gap above) whether `create_tpsl` is accepted on a flat position's id before a
         limit entry fills. If it is, sl2 could go on CoinDCX before the fill.
3. Set `TRADING_MODE=live` in `.env` and run `sudo systemctl restart strata@coindcx_trades`.

## Code map

| File | What |
|---|---|
| `strata/futures/plan.py` | `TradePlan`: config, validation, sizing, take-profit split. `Instrument`: exchange rules. |
| `strata/futures/stops.py` | `TimedStop`: the sl1/sl2/timer rules, pure logic. |
| `strata/futures/trader.py` | `TradeManager`: runs all trades, the state machine, persistence, CSV, `run()`. |
| `strata/futures/executors.py` | `PaperExecutor` (simulated fills) and `LiveExecutor` (CoinDCX orders). |
| `strata/futures/coindcx_futures.py` | The CoinDCX REST client, with notes on verified API behaviour. |
| `tests/test_timed_sl.py` | All three stop scenarios, take profits, limits, shorts, restarts, live safety. |
| `config/trades.example.yaml` | Template; the live file `config/trades.yaml` exists only on the server. |

## Extending

- **Move the stop after a take profit:** `TradeManager.on_take_profit()` in `trader.py`. Change
  `st.sl1` / `st.sl2` there; the timed stop reads them on every tick. If sl2 moves, also update the
  exchange stop: cancel the old whole-position SL, then call `self.ex.protect()`. Add a per-trade
  option through `TradePlan.extra`, which keeps unknown config keys.
- **Alerts:** call a notifier from `TradeManager.record()` (every fill) and from the
  `TIMER_STARTED` branch in `manage()`.
- **Exact ticks:** prices come from `/data/trades` (the last 30 trades, about 2 s of BTC), polled
  every second. A burst can skip ticks. CoinDCX recommends its futures websocket; swap it in behind
  `Executor.ticks()`.

## CoinDCX API facts used (docs.coindcx.com, 2026-11-17)

- Orders: `POST .../futures/orders/create` with `{"order": {...}}`. Market orders have no price and
  no `time_in_force`. `margin_currency_short_name` is USDT (default) or INR. Leverage must equal
  the position's saved leverage, so Strata calls `positions/update_leverage` first.
- List orders needs `status` and `side`. Positions, orders and cancel-all default to USDT margin,
  so pass `["INR"]` for INR trades.
- `create_tpsl`: only `take_profit_market` / `stop_market`, on the **whole** position. One of each
  per position ("TP already exists").
- `positions/exit` closes the whole position. `cancel_all_open_orders_for_position` clears its
  orders.
- INR margin rate: `POST /api/v1/derivatives/futures/data/conversions` (signed).
- Not documented, so check on the first live trade: whether create_tpsl orders vanish when the
  position closes, and whether the exchange stop triggers on the last price or the mark price.
