# Strata

A small trading bot for the home server (`homeserver`, Ubuntu 26.04). It covers **Phase 5** of the
home-server plan: each platform runs as its own systemd service, starts at boot, restarts 10 s
after a crash, and keeps API keys in `.env`, never in the code.

> **It starts in paper mode.** Orders are simulated on real market prices until you set
> `TRADING_MODE=live` in `.env`. Don't do that until a static IP is registered with Zerodha
> (plan Phase 5, step 5) and paper results have earned it.

## Install on the server (Phase 5, steps 1–4)

```bash
ssh ishwar@homeserver
git clone https://github.com/phoenix94-design/Trading_strata.git ~/strata
cd ~/strata
bash deploy/install.sh coindcx            # add "zerodha" once Kite keys are in .env
journalctl -u 'strata@*' -f               # watch it work (Ctrl+C to stop watching)
```

`install.sh` creates the venv, installs packages, creates `.env` from `.env.example` with
`chmod 600`, installs the `strata@.service` template and starts one copy per platform.
After any `git pull`, run it again to apply the update.

## Daily use

| Task | Command |
|---|---|
| Live logs | `journalctl -u 'strata@*' -f` |
| Trade history | `column -s, -t < logs/trades_coindcx.csv` |
| Stop all new entries (open trades keep their stops) | `touch state/KILL` (`rm state/KILL` to resume) |
| Stop a platform completely | `sudo systemctl stop strata@coindcx` |
| Zerodha morning login (tokens expire ~6 AM daily) | `venv/bin/python -m strata.zerodha_login` |
| Backtest | `venv/bin/python -m strata.backtest --platform coindcx --symbol I-BTC_INR --candles 1000` |

## How it trades

* **Strategy `ema_trend`** (`strata/strategies/ema_trend.py`): long only. Buy when EMA20 crosses
  above EMA50 while price is above EMA200; stop at 2.5×ATR below entry, trailed up; exit on
  the cross back down. Acts only on *closed* candles (1 h by default).
* **Risk** (`config/<platform>.yaml`): a trade that hits its stop loses about 1% of the account;
  caps per position and on open positions; no new entries after a 3% losing day.
* **Stops live at the broker where possible.** Zerodha: every buy gets a GTT stop-loss on
  Zerodha's servers, so it fires even if the house loses power. CoinDCX spot has no usable
  stop order, so Strata checks the stop every 60 s.
* **Restart-safe.** State is in `state/` (atomic writes); after a crash or power cut it carries on
  and never acts twice on the same candle. On Zerodha it reconciles with holdings, so a GTT that
  fired while the server was down gets recorded.

## CoinDCX futures price-level bot

Set an entry price, take profit and stop loss in `config/coindcx_futures.yaml`. The bot watches 24/7
and acts on them, with TP/SL held on CoinDCX. Run it with `bash deploy/install.sh coindcx_futures`.
Full handoff, including the live checklist: [docs/COINDCX_FUTURES.md](docs/COINDCX_FUTURES.md).

## Healthchecks (Phase 6)

Put one ping URL per platform in `.env` (`HC_PING_COINDCX=https://hc-ping.com/...`). Strata pings
after every completed loop, so an alert means the *app* stopped, not just the machine. It also
sends a `fail` ping when a loop errors, and during market hours when the Kite login is missing.
That doubles as your morning "log in to Kite" reminder.

## Layout

```
main.py                    entry point: --platform coindcx|zerodha|coindcx_futures [--once]
config/*.yaml              symbols, candle interval, strategy params, risk limits
strata/engine.py           the loop: reconcile -> stops -> strategy -> orders
strata/strategies/         strategies (add new ones and register in __init__.py)
strata/brokers/            paper, coindcx, zerodha
strata/risk.py             position sizing, daily loss limit, kill switch
strata/backtest.py         offline testing
strata/futures/            CoinDCX futures client + price-level strategy
deploy/                    strata@.service template + install.sh
tests/                     pytest suite
```
