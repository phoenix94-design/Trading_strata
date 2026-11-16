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
python3 -m venv --help >/dev/null 2>&1 || sudo apt install -y python3-venv
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
