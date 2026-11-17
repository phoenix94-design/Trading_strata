# CoinDCX futures price-level bot: handoff

For the Claude session (or person) running the home server. This document is the whole job:
copy the code, set the prices, start the service.

## What it does

Watches a CoinDCX USDT futures price every 5 seconds, 24/7. For each setup in
`config/coindcx_futures.yaml`:

1. **waiting**: does nothing until the price reaches `entry_price` (or, with `entry_price: market`, enters at once while the price is between stop and target).
2. **open**: opens the position at market (long = buy, short = sell) with the set leverage
   (isolated margin), then immediately puts a **take-profit and stop-loss on CoinDCX itself**
   (`create_tpsl`). If CoinDCX rejects the TP/SL, the bot closes the position straight away.
3. **closed**: when TP or SL fires, the trade goes into `logs/trades_coindcx_futures.csv`. The
   setup then ends (`repeat: false`), or goes back to waiting (`repeat: true`).

Safety built in: stop-loss is required; prices must be in the right order; leverage capped at
10x; config refused if the stop is beyond the approximate liquidation distance; no entry if
price is already past the stop or target; after a restart it adopts any open CoinDCX position
for the pair instead of buying again; backup exit if price is 0.5% past the stop and the
position is still open.

## Files

| File | Role |
|---|---|
| `strata/futures/price_levels.py` | the strategy (state machine, paper + live) |
| `strata/futures/coindcx_futures.py` | CoinDCX futures REST client |
| `config/coindcx_futures.yaml` | **your prices**: entry, take profit, stop loss, quantity, leverage |
| `tests/test_futures.py` | tests (paper flow, live flow with a fake exchange, safety paths) |
| `state/futures_price_levels.json` | runtime state (created automatically, not in git) |
| `logs/trades_coindcx_futures.csv` | every open and close |

## Deploy on homeserver

```bash
cd ~/strata && git fetch origin && git checkout claude/home-server-phase-5-g2lfr1 && git pull
bash deploy/install.sh coindcx_futures
venv/bin/python -m pytest -q tests          # needs: venv/bin/pip install pytest
journalctl -u strata@coindcx_futures -f
```
You should see `starting in PAPER mode: btc_dip_long(...)`, then a `prices {...}` line every
5 minutes. A `loop failed` line means CoinDCX couldn't be reached. Investigate if it repeats.

## Change the prices

Edit `config/coindcx_futures.yaml`, then `sudo systemctl restart strata@coindcx_futures`.
**The prices shipped in the file are placeholders. Set them from the current market first.**
To restart a setup that is `done`, delete `state/futures_price_levels.json` while the service is
stopped (only when no position is open).

## Going live (only when the user says so)

1. CoinDCX API key with **futures trading** permission; put `COINDCX_API_KEY` /
   `COINDCX_API_SECRET` in `.env` (`chmod 600 .env`).
2. Small first live test: smallest `quantity` CoinDCX allows, `leverage: 1`, entry close to the
   market price.
3. Set `TRADING_MODE=live` in `.env`, restart, and check on the CoinDCX app that:
   - [ ] the position opened with the right side, size and leverage
   - [ ] TP and SL show on the position at the configured prices
   - [ ] after a close, the CSV gets a `close` row and the setup goes `done`/`waiting`
4. If an order is rejected, read the error in `journalctl`. The likely fixes are in the next section.

## Unverified API details

docs.coindcx.com couldn't be reached when this was written. The endpoints follow the open-source
client github.com/anujsainicse/coindcx-futures. Confirm these on the first live test:

- Order body is `{"order": {...}, "timestamp": ms}` with `order_type: "market_order"`,
  `notification: "email_notification"`, `time_in_force: "good_till_cancel"`,
  `margin_currency_short_name: "USDT"`, `position_margin_type: "isolated"`.
- Price: last trade from `GET .../futures/data/trades?pair=`; `trades[0]["price"]` assumed newest.
- `price_decimals` per setup: if CoinDCX rejects the TP/SL price precision, lower it (BTC: 1, ETH: 2).
- Leverage: there's no update-leverage call. If a pair already has a position at another
  leverage, CoinDCX may reject the order; close it or match the leverage.
- Positions: `POST .../futures/positions` with `page`/`size`; fields `id`, `pair`, `active_pos`,
  `avg_price`.
