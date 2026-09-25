#!/usr/bin/env bash
# Run on the GCP VM after cloning the repo.
# Prefer: sudo bash deploy/setup-server.sh
# If you do not have sudo, use deploy/watchdog.sh via a startup script instead.
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

echo "==> Installing systemd services (bots + dashboard)"
USER_NAME="$(whoami)"
install_unit() {
  local src="$1"
  local name="$2"
  local dst="/etc/systemd/system/$name"
  sed -e "s|WorkingDirectory=.*|WorkingDirectory=$ROOT|" \
      -e "s|User=.*|User=$USER_NAME|" \
      -e "s|Environment=PYTHONPATH=.*|Environment=PYTHONPATH=$ROOT/src|" \
      "$src" | sudo tee "$dst" >/dev/null
  # Fix ExecStart paths inside the rewritten unit
  if [[ "$name" == "pmcup-bots.service" ]]; then
    sudo sed -i "s|ExecStart=.*|ExecStart=$ROOT/.venv/bin/python -m pmcup.bots.daemon|" "$dst"
  else
    sudo sed -i "s|ExecStart=.*|ExecStart=$ROOT/.venv/bin/python -m pmcup dashboard --host 127.0.0.1 --port 8080|" "$dst"
  fi
}

install_unit "$ROOT/deploy/pmcup-bots.service" "pmcup-bots.service"
install_unit "$ROOT/deploy/pmcup-dashboard.service" "pmcup-dashboard.service"

echo "==> Bootstrapping fair probs + forecasts (first-time)"
export PYTHONPATH="$ROOT/src"
"$ROOT/.venv/bin/python" -m pmcup bootstrap-probs || true
"$ROOT/.venv/bin/python" -m pmcup update-probs || true

sudo systemctl daemon-reload
sudo systemctl enable pmcup-bots pmcup-dashboard
sudo systemctl restart pmcup-bots pmcup-dashboard

echo "==> Done"
sudo systemctl status pmcup-bots --no-pager || true
sudo systemctl status pmcup-dashboard --no-pager || true
echo
echo "Logs: journalctl -u pmcup-bots -f"
echo "Or:   tail -f $ROOT/data/bots/runner.log"
echo "Dash: gcloud compute ssh pmcup-bots1 --zone=us-east4-b -- -L 8080:localhost:8080"
