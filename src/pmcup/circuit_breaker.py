from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings, settings
from .notify import notify_alert

log = logging.getLogger("pmcup.circuit")


def state_path() -> Path:
    path = Path("data/bots")
    path.mkdir(parents=True, exist_ok=True)
    return path / "circuit_breaker.json"


def _load() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {
            "peak_equity": None,
            "last_equity": None,
            "tripped": False,
            "drawdown": 0.0,
            "size_mult": 1.0,
            "updated_at": None,
            "tripped_at": None,
        }
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {
            "peak_equity": None,
            "last_equity": None,
            "tripped": False,
            "drawdown": 0.0,
            "size_mult": 1.0,
            "updated_at": None,
            "tripped_at": None,
        }


def _save(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state_path().write_text(json.dumps(state, indent=2))


def update_circuit_breaker(
    equity: float | None,
    cfg: Settings | None = None,
) -> dict[str, Any]:
    """
    Track peak equity and trip when drawdown from peak >= threshold.

    When tripped: size_mult = circuit_breaker_size_mult (default 0.5).
    Hysteresis: stay tripped until drawdown falls below half the threshold.
    """
    cfg = cfg or settings
    state = _load()
    if not cfg.circuit_breaker_enabled:
        state.update(
            {
                "tripped": False,
                "size_mult": 1.0,
                "drawdown": 0.0,
                "last_equity": equity,
                "enabled": False,
            }
        )
        _save(state)
        return state

    state["enabled"] = True
    if equity is None or equity <= 0:
        state["last_equity"] = equity
        _save(state)
        return state

    peak = state.get("peak_equity")
    if peak is None or equity > float(peak):
        peak = float(equity)
    state["peak_equity"] = float(peak)
    state["last_equity"] = float(equity)

    drawdown = 0.0 if peak <= 0 else max(0.0, (float(peak) - float(equity)) / float(peak))
    state["drawdown"] = round(drawdown, 6)

    threshold = float(cfg.circuit_breaker_drawdown)
    recover = threshold * 0.5
    was_tripped = bool(state.get("tripped"))

    if was_tripped:
        tripped = drawdown >= recover
    else:
        tripped = drawdown >= threshold

    state["tripped"] = tripped
    state["size_mult"] = (
        float(cfg.circuit_breaker_size_mult) if tripped else 1.0
    )

    if tripped and not was_tripped:
        state["tripped_at"] = datetime.now(timezone.utc).isoformat()
        log.warning(
            "Circuit breaker TRIPPED: drawdown=%.1f%% peak=%.0f equity=%.0f → size×%.2f",
            drawdown * 100,
            peak,
            equity,
            state["size_mult"],
        )
        try:
            notify_alert(
                "[Predictions Cup] Circuit breaker tripped",
                (
                    f"Equity drew down {drawdown:.1%} from peak.\n"
                    f"Peak: {peak:,.0f}\n"
                    f"Now:  {equity:,.0f}\n"
                    f"New buys sized at {state['size_mult']:.0%} until recovery.\n"
                ),
                cfg=cfg,
            )
        except Exception:  # noqa: BLE001
            log.exception("Circuit-breaker notify failed")
    elif was_tripped and not tripped:
        state["tripped_at"] = None
        log.info(
            "Circuit breaker RESET: drawdown=%.1f%% peak=%.0f equity=%.0f",
            drawdown * 100,
            peak,
            equity,
        )

    _save(state)
    return state


def current_size_mult(cfg: Settings | None = None) -> float:
    cfg = cfg or settings
    if not cfg.circuit_breaker_enabled:
        return 1.0
    state = _load()
    if state.get("tripped"):
        return float(cfg.circuit_breaker_size_mult)
    return 1.0


def circuit_status() -> dict[str, Any]:
    return _load()
