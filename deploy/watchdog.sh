#!/usr/bin/env bash
# No-sudo keeper for GCP e2-micro: restart bots + dashboard if they die.
# Recommended: add as a Compute Engine startup-script, or run under nohup.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data/bots

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
export PYTHONPATH="$ROOT/src"
export PYTHONUNBUFFERED=1

start_bots() {
  if [[ -f data/bots/runner.pid ]] && kill -0 "$(cat data/bots/runner.pid)" 2>/dev/null; then
    return 0
  fi
  nohup python -m pmcup.bots.daemon >> data/bots/runner.out 2>&1 &
  echo $! > data/bots/runner.pid
  echo "$(date -u +%FT%TZ) started bots pid=$(cat data/bots/runner.pid)" >> data/bots/watchdog.log
}

start_dashboard() {
  if [[ -f data/bots/dashboard.pid ]] && kill -0 "$(cat data/bots/dashboard.pid)" 2>/dev/null; then
    return 0
  fi
  nohup python -m pmcup dashboard --host 127.0.0.1 --port 8080 >> data/bots/dashboard.log 2>&1 &
  echo $! > data/bots/dashboard.pid
  echo "$(date -u +%FT%TZ) started dashboard pid=$(cat data/bots/dashboard.pid)" >> data/bots/watchdog.log
}

start_bots
start_dashboard

while true; do
  start_bots
  start_dashboard
  sleep 60
done
