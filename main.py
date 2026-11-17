"""Strata entry point.

    venv/bin/python main.py --platform coindcx          # run forever (what systemd does)
    venv/bin/python main.py --platform coindcx --once   # one loop, then exit (for testing)
    venv/bin/python main.py --platform coindcx_futures  # price-level futures bot (config/coindcx_futures.yaml)
    venv/bin/python main.py --platform coindcx_trades   # timed-stop trader (config/trades.yaml)
"""
import argparse
import logging
import os
import sys

from strata import brokers
from strata.config import ROOT, load_settings, read_env_file
from strata.engine import Engine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=["zerodha", "coindcx", "coindcx_futures", "coindcx_trades"])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "state").mkdir(exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout),                     # -> journalctl
                logging.FileHandler(ROOT / "logs" / f"strata_{args.platform}.log")]
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.platform in ("coindcx_futures", "coindcx_trades"):
        env = {**read_env_file(ROOT / ".env"), **os.environ}
        if args.platform == "coindcx_futures":
            from strata.futures.price_levels import run
        else:
            from strata.futures.trader import run
        run(ROOT, env)
        return

    s = load_settings(args.platform)

    broker, data = brokers.build(args.platform, s, s.state_dir)
    engine = Engine(s, broker, data)
    if args.once:
        print(engine.tick())
    else:
        engine.run_forever(int(s.cfg.get("poll_seconds", 60)))


if __name__ == "__main__":
    main()
