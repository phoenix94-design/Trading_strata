#!/usr/bin/env bash
# Phase 5 installer. Run ON THE SERVER from the project folder:
#   cd ~/strata && bash deploy/install.sh coindcx          # or: coindcx zerodha
# Safe to run again after every `git pull`.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
USER_NAME="$(id -un)"
PLATFORMS=("${@:-coindcx}")

echo "==> 1. Python environment"
# `venv --help` works even without ensurepip, so test ensurepip itself (Ubuntu 26.04: python3.14-venv)
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  sudo apt-get install -y "python${PYV}-venv" || sudo apt-get install -y python3-venv
  rm -rf venv                                  # a half-built venv from a failed run
fi
[ -x venv/bin/pip ] || rm -rf venv
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
mkdir -p state logs

echo "==> 2. Secrets file"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    created .env from .env.example (paper mode, no keys yet)"
fi
chmod 600 .env
grep -q '^TRADING_MODE=live' .env && echo "    !! .env says TRADING_MODE=live: real orders will be sent" || echo "    mode: paper"

if [ ! -f config/trades.yaml ] && [ -f config/trades.example.yaml ]; then
  cp config/trades.example.yaml config/trades.yaml
  echo "    created config/trades.yaml from the example (your trades go here; it is not in git)"
fi

echo "==> 3. systemd service"
sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" deploy/strata@.service \
  | sudo tee /etc/systemd/system/strata@.service >/dev/null
sudo systemctl daemon-reload
for p in "${PLATFORMS[@]}"; do
  sudo systemctl enable "strata@$p" >/dev/null 2>&1
  sudo systemctl restart "strata@$p"
  echo "    strata@$p enabled and (re)started"
done

sleep 3
for p in "${PLATFORMS[@]}"; do systemctl --no-pager --lines=5 status "strata@$p" || true; done
echo
echo "Watch live logs:   journalctl -u 'strata@*' -f"
echo "Trades so far:     column -s, -t < logs/trades_<platform>.csv"
echo "Stop new entries:  touch state/KILL     (rm state/KILL to resume)"
