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
from ..circuit_breaker import update_circuit_breaker
from ..equity import record_equity_point
from ..forecasts import update_fair_probs_from_forecasts
from ..notify import notify_bots_stopped, notify_cycle_failures
from ..paper_scoreboard import summarize_paper
from ..research import snapshot_scan
from ..portfolio import cash_and_equity
from ..scanner import scan
from ..trading_window import window_status
from .executor import Executor, bots_dir
from .risk import cash_buffer_proposals, risk_exit_proposals
from .rotation import (
    record_fills as record_rotation_fills,
    filter_buys as filter_rotation_buys,
    load_state as load_rotation_state,
    prune as prune_rotation,
    rotation_proposals,
    save_state as save_rotation_state,
)
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
    forecast_meta: dict[str, Any] = {}
    if cfg.bot_refresh_forecasts_every_n > 0 and cycle % cfg.bot_refresh_forecasts_every_n == 0:
        try:
            forecast_meta = update_fair_probs_from_forecasts(
                protect_manual=cfg.protect_manual_fair_probs
            )
            log.info(
                "Refreshed forecasts: matched=%s protected=%s",
                forecast_meta.get("matched"),
                forecast_meta.get("protected"),
            )
        except Exception:  # noqa: BLE001
            log.exception("Forecast refresh failed")

    result = scan(cfg)
    ideas = result["ideas"]
    proposals: list[OrderProposal] = []

    if cfg.bot_enable_edge:
        proposals.extend(proposals_from_edge_ideas(ideas, cfg=cfg))
    if cfg.bot_enable_constraint:
        proposals.extend(proposals_from_constraints(ideas))

    # Prefer positions already fetched in scan (avoids a duplicate API call)
    positions: dict[str, Any] = result.get("positions") or {"positions": []}
    breaker: dict[str, Any] = {"tripped": False, "size_mult": 1.0, "drawdown": 0.0}
    with SuperMarketClient(cfg) as client:
        if not (positions.get("positions") or positions.get("summary")):
            try:
                positions = client.positions(result["slug"])
            except Exception:  # noqa: BLE001
                log.exception("Could not load positions")
                positions = {"positions": []}
        # Equity / cash first — risk + cash-buffer sells need them
        paper_pre: dict[str, Any] = {}
        try:
            paper_pre = summarize_paper()
        except Exception:  # noqa: BLE001
            paper_pre = {}
        acct = result.get("account") or {}
        cash_f, equity_from_pos = cash_and_equity(
            acct, positions, fallback=float(result.get("bankroll") or 100_000)
        )
        if result.get("cash") is not None:
            try:
                cash_f = float(result["cash"])
            except (TypeError, ValueError):
                pass
        equity_now: float | None
        if cfg.can_trade_live:
            equity_now = float(result.get("equity") or equity_from_pos)
        else:
            equity_now = cash_f + float(paper_pre.get("total_pnl") or 0)

        rotation_state = prune_rotation(load_rotation_state())
        if cfg.bot_enable_risk:
            proposals.extend(risk_exit_proposals(positions, ideas, cfg))
            if float(getattr(cfg, "target_cash_frac", 0.0) or 0.0) > 0:
                proposals.extend(
                    cash_buffer_proposals(
                        positions,
                        cash=cash_f,
                        equity=float(equity_now or 0),
                        cfg=cfg,
                    )
                )
            rotations, rotation_state = rotation_proposals(
                positions,
                ideas,
                cfg,
                equity=float(equity_now or 0),
                state=rotation_state,
            )
            proposals.extend(rotations)

        # Cooldowns and earmarks: never buy back what we just sold
        proposals = filter_rotation_buys(proposals, cfg, state=rotation_state)

        buys = [p for p in proposals if p.action == "buy"]
        sells = [p for p in proposals if p.action == "sell"]
        best_buy: dict[str, OrderProposal] = {}
        for p in buys:
            key = p.race_key or p.key
            prev = best_buy.get(key)
            if prev is None or p.priority > prev.priority:
                best_buy[key] = p
        # Tiny leftover trims shouldn't eat the order budget
        meaningful_sells = [p for p in sells if p.quantity >= 25]
        # When cash is below target, sell first so same-cycle buys can reuse cash
        target_frac = float(getattr(cfg, "target_cash_frac", 0.0) or 0.0)
        cash_short = (
            target_frac > 0
            and float(equity_now or 0) > 0
            and cash_f < float(equity_now or 0) * target_frac
        )
        if cash_short:
            merged = meaningful_sells + list(best_buy.values())
        else:
            merged = list(best_buy.values()) + meaningful_sells

        sizing_bankroll = max(
            float(result.get("bankroll") or 0),
            float(equity_now or 0),
            cash_f,
        )
        log.info(
            "Sizing bankroll=%.0f cash=%.0f equity=%.0f cash_short=%s",
            sizing_bankroll,
            cash_f,
            float(equity_now or 0),
            cash_short,
        )

        breaker = update_circuit_breaker(equity_now, cfg)
        size_mult = float(breaker.get("size_mult") or 1.0)

        executor = Executor(
            client,
            cfg,
            result["tournament_id"],
            bankroll=sizing_bankroll,
            positions_payload=positions,
            size_mult=size_mult,
            cash_available=cash_f,
        )
        executed = executor.execute(merged)

        # Lock out re-entry on anything we just sold, and retire filled earmarks
        try:
            rotation_state = record_rotation_fills(executed, cfg, state=rotation_state)
            save_rotation_state(rotation_state)
        except Exception:  # noqa: BLE001
            log.exception("Could not persist rotation state")

    try:
        snapshot_scan(result)
    except Exception:  # noqa: BLE001
        log.exception("Snapshot failed")

    paper = {}
    try:
        paper = summarize_paper()
    except Exception:  # noqa: BLE001
        log.exception("Paper summary failed")

    try:
        acct = result.get("account") or {}
        summary = positions.get("summary") or {}
        cash = acct.get("balance")
        if cash is None:
            cash = result.get("bankroll")
        record_equity_point(
            cash=float(cash) if cash is not None else None,
            positions_mv=(
                float(summary["totalMarketValue"])
                if summary.get("totalMarketValue") is not None
                else None
            ),
            upnl=(
                float(summary["totalUnrealizedPnl"])
                if summary.get("totalUnrealizedPnl") is not None
                else None
            ),
            rank=(result.get("leaderboard") or {}).get("myRank"),
            paper_pnl=paper.get("total_pnl"),
            live=cfg.can_trade_live,
        )
    except Exception:  # noqa: BLE001
        log.exception("Equity point failed")

    summary = {
        "cycle": cycle,
        "slug": result["slug"],
        "live": cfg.can_trade_live,
        "latches_live": cfg.latches_allow_live,
        "trading_window": window_status(),
        "proposals": len(merged),
        "executed": len(executed),
        "ok": sum(1 for r in executed if r.get("ok")),
        "fail": sum(1 for r in executed if not r.get("ok")),
        "sizing_bankroll": sizing_bankroll,
        "cash": cash_f,
        "equity": equity_now,
        "missing_forecasts": result.get("missing_forecasts") or [],
        "missing_forecast_count": len(result.get("missing_forecasts") or []),
        "forecast_refresh": {
            "matched": forecast_meta.get("matched"),
            "protected": forecast_meta.get("protected"),
            "missing_governor_ids": forecast_meta.get("missing_governor_ids"),
        },
        "paper": paper,
        "circuit_breaker": {
            "enabled": breaker.get("enabled", cfg.circuit_breaker_enabled),
            "tripped": bool(breaker.get("tripped")),
            "drawdown": breaker.get("drawdown"),
            "size_mult": breaker.get("size_mult"),
            "peak_equity": breaker.get("peak_equity"),
            "last_equity": breaker.get("last_equity"),
        },
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
        "Cycle %s done | live=%s proposals=%s executed=%s missing_forecasts=%s circuit=%s",
        cycle,
        cfg.can_trade_live,
        len(merged),
        len(executed),
        summary["missing_forecast_count"],
        "TRIPPED" if breaker.get("tripped") else "ok",
    )
    return summary


def run_forever(cfg: Settings | None = None) -> None:
    cfg = cfg or settings
    log_path = setup_logging()
    pid_path().write_text(str(os.getpid()))
    log.info(
        "Bot runner started pid=%s interval=%ss live=%s dry_run=%s window=%s log=%s",
        os.getpid(),
        cfg.bot_interval_seconds,
        cfg.can_trade_live,
        cfg.dry_run,
        window_status().get("phase"),
        log_path,
    )

    state: dict[str, Any] = {
        "cycle": 0,
        "stop_reason": "unknown",
        "fail_streak": 0,
        "notified_fail_streak": 0,
    }

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
                sleep_s = max(30, int(cfg.bot_interval_seconds))
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001
                state["fail_streak"] = int(state["fail_streak"]) + 1
                log.exception("Cycle %s failed", state["cycle"])
                write_status(
                    {
                        "cycle": state["cycle"],
                        "error": str(exc),
                        "error_code": "cycle_failed",
                        "live": cfg.can_trade_live,
                        "fail_streak": state["fail_streak"],
                        "trading_window": window_status(),
                    }
                )
                # Notify once per streak threshold; keep running for infra durability
                streak = int(state["fail_streak"])
                threshold = max(1, int(cfg.bot_fail_notify_streak))
                if streak >= threshold and streak != int(state["notified_fail_streak"]):
                    try:
                        notify_cycle_failures(streak, cycle=int(state["cycle"]), error=str(exc))
                        state["notified_fail_streak"] = streak
                    except Exception:  # noqa: BLE001
                        log.exception("Fail-streak notify failed")
                sleep_s = max(
                    int(cfg.bot_fail_backoff_s),
                    int(cfg.bot_interval_seconds),
                ) * min(streak, 5)
            state["cycle"] = int(state["cycle"]) + 1
            time.sleep(sleep_s)
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
