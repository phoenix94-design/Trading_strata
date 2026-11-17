# Status: where things stand

Updated 2026-11-17. Keep this short. Details live in the linked docs.

## Done
- Phase 5 on the home server: Strata installed as systemd services, all in **paper mode** (no keys).
- **Timed-stop trader** (`strata@coindcx_trades`, [TIMED_SL_STRATEGY.md](TIMED_SL_STRATEGY.md)): built,
  44 tests, and checked in paper mode on live CoinDCX prices:
  - sl2 close
  - one-tick timer reset (6 in a row)
  - timer expiry closing at market
  - a partial take profit
  - INR-margin sizing
  - a restart mid-trade
- Earlier simple price-level bot (`strata@coindcx_futures`): idle, paper.

## Next (in order)
1. **CoinDCX API key.** IP binding is mandatory for new keys (CoinDCX guide, 3 Nov 2026). The home
   connection's public IP is dynamic (it changes when the router restarts), so a key bound to it
   only works until the next change. Recreating a key is free and quick: fine for a first test.
2. **Static IP decision**, needed anyway for Zerodha, which allows only one IP change per calendar
   week: Airtel's static-IP add-on (preferred if the price is sensible), or a small Mumbai cloud
   server used as a fixed exit for order traffic only. The VPS route needs a proxy setting added to
   the CoinDCX client.
3. Put the key in `~/strata/.env` on the server (`chmod 600`). It must never go in chat or in git.
4. First live trade: tiny size, low leverage, then follow the checklist in
   [TIMED_SL_STRATEGY.md](TIMED_SL_STRATEGY.md#going-live-only-when-ishwar-says-so).

## Later (agreed)
- Move sl1/sl2 after a take profit: hook `TradeManager.on_take_profit()`.
- Telegram alerts.
- Alert (Healthchecks fail ping) when private API calls keep failing, e.g. after an IP change.
- Futures websocket instead of polling `/data/trades`.

## Not possible
- The app's "Global Futures" (S&P500, NSDQ100, NVDA, pre-IPO) are not in CoinDCX's API.
