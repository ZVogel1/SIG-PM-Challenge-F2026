#!/usr/bin/env bash
# Deploy by pulling from git rather than hand-copying files, so the box and
# main cannot silently drift. Rolls back and leaves the old code running if
# the new commit does not even import.
#
#   ./deploy/update.sh          # deploy origin/main
#   ./deploy/update.sh --check  # report drift, change nothing
set -euo pipefail

ROOT="${PMCUP_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

# bash reads a script lazily, by byte offset, so the `git reset` below would
# make a changed update.sh resume mid-deploy at the wrong position. Run from a
# throwaway copy that git cannot touch. Unlinking at exit is safe on Linux
# because the interpreter already holds the descriptor.
if [[ "${PMCUP_DEPLOY_REEXEC:-}" != "1" ]]; then
  self="$(mktemp -t pmcup_update.XXXXXX)"
  cp "$0" "$self"
  chmod +x "$self"
  PMCUP_DEPLOY_REEXEC=1 PMCUP_ROOT="$ROOT" exec "$self" "$@"
fi
trap 'rm -f "$0"' EXIT

cd "$ROOT"
BRANCH="${BRANCH:-main}"
git fetch origin "$BRANCH"

if [[ "${1:-}" == "--check" ]]; then
  echo "local  $(git rev-parse --short HEAD) $(git log -1 --format=%s)"
  echo "origin $(git rev-parse --short "origin/$BRANCH") $(git log -1 --format=%s "origin/$BRANCH")"
  if git diff --quiet "origin/$BRANCH" -- src deploy; then
    echo "code matches origin/$BRANCH"
  else
    echo "DRIFT vs origin/$BRANCH:"
    git --no-pager diff --stat "origin/$BRANCH" -- src deploy
  fi
  exit 0
fi

OLD="$(git rev-parse HEAD)"

# Runtime state (data/bots) and secrets (.env) are gitignored, so a hard reset
# only touches code. A merge would stall on any file hand-edited on the box.
git reset --hard "origin/$BRANCH"
NEW="$(git rev-parse HEAD)"

rollback() {
  echo "ERROR: $1 — rolling back to ${OLD:0:7}, leaving processes untouched" >&2
  git reset --hard "$OLD"
  exit 1
}

[[ -f .env ]] || rollback ".env is missing; refusing to run with defaults"

export PYTHONPATH="$ROOT/src"
PY="$ROOT/.venv/bin/python"

# Cheapest gate that catches the realistic deploy failure: a module that no
# longer imports would otherwise crash-loop under the watchdog.
"$PY" -c "import pmcup.cli, pmcup.guard, pmcup.bots.runner, pmcup.bots.rotation, pmcup.bots.executor" \
  || rollback "new code does not import"

# Full suite when pytest is installed; skipped rather than required, since the
# trading box is not where tests are meant to be run.
if "$PY" -m pytest --version >/dev/null 2>&1; then
  "$PY" -m pytest -q || rollback "tests fail at ${NEW:0:7}"
fi

if [[ "$OLD" == "$NEW" ]]; then
  echo "already at ${NEW:0:7} — nothing to restart"
  exit 0
fi

# Stop the workers; the watchdog restarts whatever it finds dead within 60s.
for name in runner guard dashboard; do
  pid="$(cat "data/bots/$name.pid" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && echo "stopped $name ($pid)"
  fi
done

# Same byte-offset hazard as above: a rewritten watchdog.sh cannot be left to
# keep running. Replace it last, so the new keeper brings the workers back.
if ! git diff --quiet "$OLD" "$NEW" -- deploy/watchdog.sh; then
  wpid="$(cat data/bots/watchdog.pid 2>/dev/null || true)"
  [[ -n "$wpid" ]] && kill "$wpid" 2>/dev/null || true
  pkill -f "[w]atchdog.sh" 2>/dev/null || true
  sleep 2
  setsid "$ROOT/deploy/watchdog.sh" </dev/null >> "$ROOT/data/bots/watchdog.out" 2>&1 &
  disown || true
  echo "restarted watchdog (its script changed)"
fi

echo "deployed ${OLD:0:7} -> ${NEW:0:7}; watchdog restarts workers within 60s"
