from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..client import SuperMarketClient
from ..config import Settings, settings
from ..forecasts import update_fair_probs_from_forecasts
from ..notify import notify_bots_stopped
from ..research import snapshot_scan
from ..scanner import scan
from .executor import Executor, bots_dir
from .risk import risk_exit_proposals
from .strategies import proposals_from_constraints, proposals_from_edge_ideas
from .types import OrderProposal

log = logging.getLogger("pmcup.bots")


def setup_logging() -> Path:
    import sys

    bots_dir().mkdir(parents=True, exist_ok=True)
    log_path = bots_dir() / "runner.log"
    root = logging.getLogger("pmcup")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(log_path)
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(fh)
    # Only echo to console when interactive — avoids duplicate lines when
    # daemon stdout is already redirected into runner.log
    if sys.stdout.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        root.addHandler(sh)
    return log_path


def pid_path() -> Path:
    return bots_dir() / "runner.pid"


def status_path() -> Path:
    return bots_dir() / "status.json"


def write_status(payload: dict[str, Any]) -> None:
    import json

    payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    status_path().write_text(json.dumps(payload, indent=2))


def run_cycle(cfg: Settings, *, cycle: int = 0) -> dict[str, Any]:
    """One multi-bot decision cycle."""
    if cfg.bot_refresh_forecasts_every_n > 0 and cycle % cfg.bot_refresh_forecasts_every_n == 0:
        try:
            upd = update_fair_probs_from_forecasts()
            log.info("Refreshed forecasts: %s rows", upd["matched"])
        except Exception:  # noqa: BLE001
            log.exception("Forecast refresh failed")

    result = scan(cfg)
    ideas = result["ideas"]
    proposals: list[OrderProposal] = []

    if cfg.bot_enable_edge:
        proposals.extend(proposals_from_edge_ideas(ideas))
    if cfg.bot_enable_constraint:
        proposals.extend(proposals_from_constraints(ideas))

    with SuperMarketClient(cfg) as client:
        if cfg.bot_enable_risk:
            try:
                positions = client.positions(result["slug"])
            except Exception:  # noqa: BLE001
                log.exception("Could not load positions")
                positions = {"positions": []}
            proposals.extend(risk_exit_proposals(positions, ideas, cfg))

        buys = [p for p in proposals if p.action == "buy"]
        sells = [p for p in proposals if p.action == "sell"]
        best_buy: dict[str, OrderProposal] = {}
        for p in buys:
            key = p.race_key or p.key
            prev = best_buy.get(key)
            if prev is None or p.priority > prev.priority:
                best_buy[key] = p
        merged = sells + list(best_buy.values())

        executor = Executor(client, cfg, result["tournament_id"])
        executed = executor.execute(merged)

    try:
        snapshot_scan(result)
    except Exception:  # noqa: BLE001
        log.exception("Snapshot failed")

    summary = {
        "cycle": cycle,
        "slug": result["slug"],
        "live": cfg.can_trade_live,
        "proposals": len(merged),
        "executed": len(executed),
        "ok": sum(1 for r in executed if r.get("ok")),
        "top": [
            {
                "bot": p.bot,
                "action": p.action,
                "side": p.side,
                "title": p.market_title,
                "qty": p.quantity,
                "priority": p.priority,
            }
            for p in sorted(merged, key=lambda x: x.priority, reverse=True)[:8]
        ],
    }
    write_status(summary)
    log.info(
        "Cycle %s done | live=%s proposals=%s executed=%s",
        cycle,
        cfg.can_trade_live,
        len(merged),
        len(executed),
    )
    return summary


def run_forever(cfg: Settings | None = None) -> None:
    cfg = cfg or settings
    log_path = setup_logging()
    pid_path().write_text(str(os.getpid()))
    log.info(
        "Bot runner started pid=%s interval=%ss live=%s dry_run=%s log=%s",
        os.getpid(),
        cfg.bot_interval_seconds,
        cfg.can_trade_live,
        cfg.dry_run,
        log_path,
    )

    state: dict[str, Any] = {"cycle": 0, "stop_reason": "unknown", "fail_streak": 0}

    def _handle_signal(signum: int, _frame: Any) -> None:
        state["stop_reason"] = f"signal {signum}"
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while True:
            try:
                run_cycle(cfg, cycle=state["cycle"])
                state["fail_streak"] = 0
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001
                state["fail_streak"] = int(state["fail_streak"]) + 1
                log.exception("Cycle %s failed", state["cycle"])
                write_status(
                    {
                        "cycle": state["cycle"],
                        "error": "cycle_failed",
                        "live": cfg.can_trade_live,
                        "fail_streak": state["fail_streak"],
                    }
                )
                # Email if cycles keep failing (likely stuck / broken)
                if state["fail_streak"] >= 3:
                    state["stop_reason"] = f"crash after {state['fail_streak']} failed cycles: {exc}"
                    break
            state["cycle"] = int(state["cycle"]) + 1
            time.sleep(max(30, int(cfg.bot_interval_seconds)))
    except SystemExit:
        if state["stop_reason"] == "unknown":
            state["stop_reason"] = "stopped"
        raise
    finally:
        reason = str(state.get("stop_reason") or "stopped")
        log.info("Bot runner stopped (%s)", reason)
        try:
            notify_bots_stopped(reason, cycle=int(state["cycle"]))
        except Exception:  # noqa: BLE001
            log.exception("Stop notification failed")
        if pid_path().exists():
            pid_path().unlink()
