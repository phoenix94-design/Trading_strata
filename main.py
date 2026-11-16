"""Strata entry point.

    venv/bin/python main.py --platform coindcx          # run forever (what systemd does)
    venv/bin/python main.py --platform coindcx --once   # one loop, then exit (for testing)
"""
import argparse
import logging
import sys

from strata import brokers
from strata.config import load_settings
from strata.engine import Engine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=["zerodha", "coindcx"])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    s = load_settings(args.platform)
    handlers = [logging.StreamHandler(sys.stdout),                     # -> journalctl
                logging.FileHandler(s.log_dir / f"strata_{args.platform}.log")]
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    broker, data = brokers.build(args.platform, s, s.state_dir)
    engine = Engine(s, broker, data)
    if args.once:
        print(engine.tick())
    else:
        engine.run_forever(int(s.cfg.get("poll_seconds", 60)))


if __name__ == "__main__":
    main()
