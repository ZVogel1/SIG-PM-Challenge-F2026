# Predictions Cup — Midterms 2026

Local + cloud trading system for [SIG Predictions Cup](https://predictionscup.com/) on [sig.thesuper.market](https://sig.thesuper.market/markets).

**Goal:** maximize chance of finishing **#1** (highest SUSQies after all markets resolve).

You do **not** need to be good at probability. You edit a spreadsheet of “what I think the chance is”; the bots do edge math, sizing, and flag structural mispricings.

Official rules: https://predictionscup.com/rules/  
API docs: https://sig.thesuper.market/api/v1/docs  
Guide: https://sig.thesuper.market/docs

Trading opens **Oct 1, 2026 12:00pm ET** and locks **Nov 4, 2026 12:00pm ET**.

---

## How it wins (simple version)

1. **Model vs market** — If you think YES is 60% and the market is 45%, buy YES. If you think 30% and market is 45%, buy NO.
2. **Don’t buy lottery tickets** — Very cheap contracts are usually overpriced. The scanner fades them unless you override.
3. **Fix inconsistent prices** — Cross-market constraint violations are high-priority trades.
4. **Size for a tournament** — Slightly more aggressive than classic Kelly, but capped so one bad race doesn’t zero you.

---

## Local laptop setup (first time)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Register at https://sig.thesuper.market
# Settings → API Keys → scopes: read + trade
# Paste key into .env  (never commit .env)

./pmcup setup
./pmcup whoami
./pmcup bootstrap-probs   # creates data/fair_probs.csv
# Edit fair_yes only where you disagree with the market
./pmcup scan              # dry-run playbook (default)
```

Live orders stay disabled until you set both latches in `.env` (see [Go live](#go-live-oct-1)).

### What you must provide

| Item | Why |
|------|-----|
| Account at [sig.thesuper.market](https://sig.thesuper.market) | Competition entry + 100k SUSQies |
| API key (`read` + `trade`) | Required by every endpoint |
| `TOURNAMENT_SLUG` in `.env` | Isolates cup order book / balance |
| Edits to `data/fair_probs.csv` | Your edge — bot can’t invent political knowledge |

Optional but powerful: spend 30–60 min/day updating fair probs on the ~20 most liquid / most mispriced races the scanner surfaces.

---

## Always-on GCP VM (recommended)

Bots run on a small e2-micro VM so trading continues when your laptop is closed.

| Setting | Value |
|---------|--------|
| GCP project | `pmcup-project` |
| VM name | `pmcup-bots1` |
| Zone | `us-east4-b` |
| Repo on VM | `~/SIG-PM-Challenge-F2026` |

### Install `gcloud` on your Mac (once)

```bash
brew install --cask google-cloud-sdk
gcloud auth login
gcloud config set project pmcup-project
gcloud compute instances list
```

### SSH into the VM

Always include the Linux username **`pmcup_zvogel`** (that’s whose home has the repo). Plain `gcloud compute ssh pmcup-bots1` logs you in as your Mac username and you’ll get “No such file”.

```bash
# shell / logs
gcloud compute ssh pmcup_zvogel@pmcup-bots1 --zone=us-east4-b

# shell + dashboard tunnel
gcloud compute ssh pmcup_zvogel@pmcup-bots1 --zone=us-east4-b -- -L 8080:localhost:8080
```

Optional Mac shortcuts (already in `~/.zshrc` if set up locally):

```bash
pmcup-ssh      # SSH as pmcup_zvogel
pmcup-dash     # SSH + port 8080 tunnel → http://127.0.0.1:8080
pmcup-status   # one-shot bot status without interactive shell
```

After `source ~/.zshrc` (or open a new terminal), use those instead of typing the long command.

### First-time (or after `git push`) on the VM

```bash
cd ~/SIG-PM-Challenge-F2026
git pull
source .venv/bin/activate
pip install -q .

# .env must exist on the VM (copy from .env.example and paste secrets — never commit it)
mkdir -p data/bots
```

### Start the bots (on the VM)

Three bots share one account and decide on an interval (`BOT_INTERVAL_SECONDS` in `.env`, often `120`):

| Bot | Job |
|-----|-----|
| `edge_hunter` | Buy when forecast ≠ market (after spread) |
| `constraint_arb` | Fix cross-market inconsistencies |
| `risk_manager` | Sell / take profit when edge flips or decays |

```bash
cd ~/SIG-PM-Challenge-F2026
source .venv/bin/activate
export PYTHONPATH=$PWD/src

# One test cycle (safe: paper/dry unless both live latches are on)
python -m pmcup bots once

# Background loop (survives closing the laptop)
nohup python -m pmcup bots run > data/bots/runner.out 2>&1 &
echo $! > data/bots/runner.pid
echo "Bots PID $(cat data/bots/runner.pid)"
```

Useful checks:

```bash
python -m pmcup bots status
tail -f data/bots/runner.log
# or: tail -f data/bots/runner.out

# Stop bots
kill "$(cat data/bots/runner.pid)" 2>/dev/null
# or: python -m pmcup bots stop
```

Logs: `data/bots/runner.log` · Decisions: `data/bots/decisions.jsonl`

### Keep-alive without sudo (recommended on this VM)

Bots must be started **detached** (not tied to your SSH window). From an SSH session:

```bash
cd ~/SIG-PM-Challenge-F2026
source .venv/bin/activate
export PYTHONPATH=$PWD/src
mkdir -p data/bots

nohup python -m pmcup.bots.daemon >> data/bots/runner.log 2>&1 </dev/null &
echo $! > data/bots/runner.pid
nohup python -m pmcup dashboard --host 127.0.0.1 --port 8080 >> data/bots/dashboard.log 2>&1 </dev/null &
echo $! > data/bots/dashboard.pid
nohup bash ./deploy/watchdog.sh >> data/bots/watchdog.out 2>&1 </dev/null &
echo $! > data/bots/watchdog.pid
disown -a
```

Confirm processes show `?` for TTY (not `pts/0`):
```bash
ps -u "$USER" -o pid=,tty=,args= | grep -E 'pmcup|watchdog' | grep -v grep
```

Closing your laptop only drops SSH/tunnel — it does **not** stop detached VM processes. To view logs again later:

```bash
gcloud compute ssh pmcup_zvogel@pmcup-bots1 --zone=us-east4-b
tail -f ~/SIG-PM-Challenge-F2026/data/bots/runner.log
```

GCP **startup-script** metadata on `pmcup-bots1` starts `deploy/watchdog.sh` after reboot.

With sudo available: `sudo bash deploy/setup-server.sh` installs `pmcup-bots` + `pmcup-dashboard` with `Restart=always`.

### Safety / edge upgrades (built-in)

- **Trading window** — live orders only Oct 1–Nov 4 2026 12:00 ET (`ENFORCE_TRADING_WINDOW`)
- **Race exposure caps** — won't stack > `MAX_RACE_EXPOSURE_FRAC` of bankroll into one contest
- **Basket caps** — House / Senate / Gov correlated exposure capped separately (`MAX_HOUSE_BASKET_FRAC`, etc.)
- **Market blend** — low-confidence forecasts shrink toward the live mid (`BLEND_TOWARD_MARKET`)
- **No live FLB** — heuristic trades without a fair_probs row stay paper-only unless `ALLOW_FLB_LIVE=true`
- **Manual fair probs protected** — set `source=manual` or `locked=true` so auto-refresh won't overwrite
- **API retries** + stable order idempotency keys
- **Runner stays up** after failed cycles (emails on streak); use watchdog/systemd for process death
- **Richer alerts** — stop, cycle failures, order failures (`NOTIFY_ON_ERRORS`)
- **Dashboard** — staleness, fail streak, missing forecasts, paper PnL scoreboard

### Start the dashboard (on the VM)

```bash
cd ~/SIG-PM-Challenge-F2026
source .venv/bin/activate
export PYTHONPATH=$PWD/src
mkdir -p data/bots

nohup python -m pmcup dashboard --host 127.0.0.1 --port 8080 > data/bots/dashboard.log 2>&1 &
echo $! > data/bots/dashboard.pid
echo "Dashboard PID $(cat data/bots/dashboard.pid)"

# Sanity check (should print 200)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/
```

Shows mode, balance, rank, positions, leaderboard, and recent bot decisions (auto-refresh every 30s).

### View the dashboard from your Mac (SSH tunnel)

1. Make sure nothing else is using port 8080 on the Mac (stop a local dashboard if needed).
2. In a **Mac** terminal, leave this open:

```bash
gcloud compute ssh pmcup-bots1 --zone=us-east4-b -- -L 8080:localhost:8080
```

3. Open Chrome: **http://127.0.0.1:8080**

That page is the **VM** copy (cloud bots + cloud logs). Leave the SSH window open while viewing; do not type `exit`.

### What keeps running when you close the laptop?

| Piece | Survives laptop close? |
|-------|------------------------|
| Bots on the VM (`nohup`) | **Yes** |
| Dashboard process on the VM (`nohup`) | **Yes** |
| SSH tunnel / viewing `127.0.0.1:8080` | **No** — reconnect the tunnel command above when you want to look again |

### Reconnect checklist (after sleep / new day)

```bash
# Mac: tunnel + optional SSH
gcloud compute ssh pmcup-bots1 --zone=us-east4-b -- -L 8080:localhost:8080

# Inside VM (or a second SSH), confirm bots still up:
cd ~/SIG-PM-Challenge-F2026 && source .venv/bin/activate
python -m pmcup bots status
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/
```

### Pull new code onto the VM

From your Mac (after committing/pushing):

```bash
git push
```

On the VM:

```bash
cd ~/SIG-PM-Challenge-F2026
git pull
source .venv/bin/activate
pip install -q .
# Restart bots / dashboard if needed (stop old PID, start again with nohup)
```

### Browser SSH (fallback without tunnel)

GCP Console → Compute Engine → `pmcup-bots1` → **SSH** works for shell access, but **does not** open the web dashboard on your Mac. Prefer the `gcloud … -L 8080:…` tunnel for the UI.

---

## Go live (Oct 1+)

In **`.env` on the VM** (and laptop if you trade from there), set **both**:

```
DRY_RUN=false
LIVE_TRADING=true
```

Restart bots so they pick up settings. Live orders still require the official window (`ENFORCE_TRADING_WINDOW=true` by default).

Protect hand-edited rows in `data/fair_probs.csv` with `source=manual` or `locked=true`.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

---

## Local-only bots / dashboard (optional)

If you are not using the VM:

```bash
source .venv/bin/activate
./pmcup bots once
./pmcup bots run          # background on this machine
./pmcup bots status
./pmcup bots stop

./pmcup dashboard --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080
```

Local dashboard uses the laptop’s `.env` and local `data/bots/` files — it is **not** the cloud bot state unless you tunnel to the VM.

On macOS, `bots run` may enable `caffeinate` so the machine doesn’t idle-sleep while the process is attached; closing the lid still usually suspends. Prefer the GCP VM for always-on.

---

## Daily research helpers

```bash
./pmcup daily
```

1. Refresh fair probs from poll/fundamentals consensus  
2. Scan for spread-aware edges  
3. Snapshot history under `data/history/`  
4. Paper-fill top ideas into `data/paper/portfolio.json`

Also useful:

- `./pmcup research` — persistence report across snapshots  
- `./pmcup paper --top 5` — paper trade only  
- `./pmcup paper --reset` — wipe paper book  
- `./pmcup update-probs` — refresh consensus into `fair_probs.csv`

---

## Commands cheat sheet

| Command | What it does |
|---------|----------------|
| `pmcup setup` | Create `.env` + example files |
| `pmcup whoami` | Verify API key, list tournaments |
| `pmcup bootstrap-probs` | Seed fair-prob CSV from live markets |
| `pmcup update-probs` | Refresh consensus fair probs |
| `pmcup scan` | Ranked plain-English trades |
| `pmcup scan --execute-top 3` | Place top 3 (only if `DRY_RUN=false`) |
| `pmcup bots once` | One multi-bot cycle |
| `pmcup bots run` | Continuous cycles |
| `pmcup bots status` / `stop` | Check / stop runner |
| `pmcup dashboard` | Live web UI on port 8080 |
| `pmcup positions` / `leaderboard` | Account snapshot |
| `pmcup daily` / `research` / `paper` | Research + paper book |

---

## Safety / rules reminders

- One account only (multi-accounting = DQ)
- No trading on material non-public info
- Bots/API are explicitly allowed
- Keep `DRY_RUN=true` until you trust the playbook
- Never commit `.env` or paste live API keys into chat/git
- Rotate keys if they were ever exposed

---

## Project layout

```
src/pmcup/
  client.py       # Super Market API
  scanner.py      # Pulls markets + builds ideas
  edge.py         # Model-vs-market + FLB heuristics
  constraints.py  # Relationship arb ideas
  sizing.py       # Tournament Kelly
  fair_probs.py   # CSV fair probabilities
  forecasts.py    # Poll / fundamentals consensus
  bots/           # edge_hunter, constraint_arb, risk_manager
  dashboard.py    # Live stats web UI
  notify.py       # Email on stop (optional SMTP)
  cli.py          # Commands
deploy/
  setup-server.sh       # Optional systemd helper (needs sudo)
  pmcup-bots.service
data/
  fair_probs.csv
  bots/           # logs, pids, decisions (on VM)
```
