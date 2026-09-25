#!/usr/bin/env bash
# Run on the Azure VM after cloning the repo.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> Creating venv + installing pmcup"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install .

if [[ ! -f .env ]]; then
  echo "==> No .env found. Copying from example — EDIT IT before starting bots."
  cp .env.example .env
  echo "Edit: nano $ROOT/.env"
  exit 1
fi

echo "==> Installing systemd service"
SERVICE_SRC="$ROOT/deploy/pmcup-bots.service"
SERVICE_DST="/etc/systemd/system/pmcup-bots.service"
# Rewrite WorkingDirectory/User for this machine
USER_NAME="$(whoami)"
sed -e "s|WorkingDirectory=.*|WorkingDirectory=$ROOT|" \
    -e "s|User=.*|User=$USER_NAME|" \
    -e "s|ExecStart=.*|ExecStart=$ROOT/.venv/bin/python -m pmcup.bots.daemon|" \
    -e "s|Environment=PYTHONPATH=.*|Environment=PYTHONPATH=$ROOT/src|" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DST" >/dev/null

echo "==> Bootstrapping fair probs + forecasts (first-time)"
export PYTHONPATH="$ROOT/src"
"$ROOT/.venv/bin/python" -m pmcup bootstrap-probs || true
"$ROOT/.venv/bin/python" -m pmcup update-probs || true

sudo systemctl daemon-reload
sudo systemctl enable pmcup-bots
sudo systemctl restart pmcup-bots

echo "==> Done"
sudo systemctl status pmcup-bots --no-pager
echo
echo "Logs: journalctl -u pmcup-bots -f"
echo "Or:   tail -f $ROOT/data/bots/runner.log"
