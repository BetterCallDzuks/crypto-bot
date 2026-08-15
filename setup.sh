#!/usr/bin/env bash
# One-time setup: create the Python virtualenv and install dependencies.
# After this, start the bot with:  pm2 start ecosystem.config.js
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Creating virtualenv (.venv)"
python3 -m venv .venv

echo "==> Installing dependencies"
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt

mkdir -p data

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from template — add your Binance API key/secret for live trading."
fi

echo
echo "Setup complete."
echo "  • Paper mode works out of the box (config.yaml: market.source = simulated)."
echo "  • Start under PM2:   pm2 start ecosystem.config.js"
echo "  • Or run directly:   ./.venv/bin/python run.py"
echo "  • Dashboard:         http://127.0.0.1:4000"
