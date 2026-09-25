# Predictions Cup — Midterms 2026

Local trading system for [SIG Predictions Cup](https://predictionscup.com/) on [sig.thesuper.market](https://sig.thesuper.market/markets).

**Goal:** maximize chance of finishing **#1** (highest SUSQies after all markets resolve).

You do **not** need to be good at probability. You edit a spreadsheet of “what I think the chance is”; the bot does edge math, sizing, and flags structural mispricings.

## How it wins (simple version)

1. **Model vs market** — If you think YES is 60% and the market is 45%, buy YES. If you think 30% and market is 45%, buy NO.
2. **Don’t buy lottery tickets** — Research shows very cheap contracts are usually *overpriced*. The scanner fades them unless you override.
3. **Fix inconsistent prices** — The platform exposes cross-market constraint violations (e.g. race prices that can’t add up). Those are high-priority trades.
4. **Size for a tournament** — Slightly more aggressive than classic Kelly, but capped so one bad race doesn’t zero you.

Official rules: https://predictionscup.com/rules/  
API docs: https://sig.thesuper.market/api/v1/docs  
Guide: https://sig.thesuper.market/docs

Trading opens **Oct 1, 2026 12:00pm ET** and locks **Nov 4, 2026 12:00pm ET**.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Register at https://sig.thesuper.market
# Settings → API Keys → scopes: read + trade
# Paste key into .env

pmcup setup
pmcup whoami
pmcup bootstrap-probs   # creates data/fair_probs.csv
# Edit fair_yes only where you disagree with the market
pmcup scan              # dry-run playbook (default)
```

Live orders stay disabled until you set `DRY_RUN=false` in `.env`.

## What you must provide

| Item | Why |
|------|-----|
| Account at [sig.thesuper.market](https://sig.thesuper.market) | Competition entry + 100k SUSQies |
| API key (`read` + `trade`) | Required by every endpoint |
| `TOURNAMENT_SLUG` in `.env` | Isolates cup order book / balance |
| Edits to `data/fair_probs.csv` | Your edge — bot can’t invent political knowledge |

Optional but powerful: spend 30–60 min/day updating fair probs on the ~20 most liquid / most mispriced races the scanner surfaces. Ignore the rest; FLB + constraints still work.

## Unattended bots (you don't need to watch all day)

Three bots share one account and decide every few minutes:

| Bot | Job |
|-----|-----|
| `edge_hunter` | Buy when forecast ≠ market (after spread) |
| `constraint_arb` | Fix cross-market inconsistencies |
| `risk_manager` | Sell / take profit when edge flips or decays |

```bash
# Test one cycle (still dry-run by default)
pmcup bots once

# Start in background (checks every 3 minutes)
pmcup bots run

# Later
pmcup bots status
pmcup bots stop
```

Logs: `data/bots/runner.log` · Decisions: `data/bots/decisions.jsonl`

**Go live only when ready (Oct 1+):** in `.env` set:
```
DRY_RUN=false
LIVE_TRADING=true
```
Both must be set — two safety latches.

## Daily automation (manual research mode)

```bash
pmcup daily
```

That will:
1. Refresh fair probs from poll/fundamentals consensus  
2. Scan for spread-aware edges (deduped Dem/Rep mirrors)  
3. Snapshot history under `data/history/`  
4. Paper-fill the top ideas into `data/paper/portfolio.json`

Also useful:
- `pmcup research` — persistence report across snapshots  
- `pmcup paper --top 5` — paper trade only  
- `pmcup paper --reset` — wipe paper book  

## Commands

- `pmcup setup` — create `.env` + example files
- `pmcup whoami` — verify key, list tournaments
- `pmcup bootstrap-probs` — seed fair-prob CSV from live markets
- `pmcup scan` — ranked plain-English trades
- `pmcup scan --execute-top 3` — place top 3 (only if `DRY_RUN=false`)
- `pmcup positions` / `pmcup leaderboard`

## Safety / rules reminders

- One account only (multi-accounting = DQ)
- No trading on material non-public info
- Bots/API are explicitly allowed
- `DRY_RUN=true` until you trust the playbook

## Project layout

```
src/pmcup/
  client.py       # Super Market API
  scanner.py      # Pulls markets + builds ideas
  edge.py         # Model-vs-market + FLB heuristics
  constraints.py  # Relationship arb ideas
  sizing.py       # Tournament Kelly
  fair_probs.py   # CSV fair probabilities
  cli.py          # Commands
```
