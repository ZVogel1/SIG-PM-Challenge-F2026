"""
Capital rotation: sell a tired position only to fund a clearly better one.

The predecessor to this module sold positions to hit a cash target, which the
buyer then spent immediately, so the bot round-tripped the same markets and paid
the spread every time. Rotation here is deliberately hard to trigger and, once
triggered, is locked in one direction:

* a market that was just sold cannot be bought back until its cooldown expires,
* cash freed by a rotation is earmarked, so only the intended target can spend it,
* rotations are capped per day,
* a failed rotation sell rolls back the earmark and the daily count so a reject
  cannot stall the book or burn the budget.

Exits stay unrestricted: a loop needs sell -> buy -> sell, and blocking re-entry
is enough. Trapping us in a dead thesis would be worse than a second sell.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..edge import race_key_from_title
from ..models import TradeIdea
from .risk import exit_limit_price
from .types import OrderProposal

log = logging.getLogger("pmcup.bots")


def state_path() -> Path:
    path = Path("data/bots")
    path.mkdir(parents=True, exist_ok=True)
    return path / "rotation.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"locks": {}, "earmarks": [], "rotations": {}}
    try:
        state = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        log.warning("Unreadable rotation state — starting fresh")
        return {"locks": {}, "earmarks": [], "rotations": {}}
    state.setdefault("locks", {})
    state.setdefault("earmarks", [])
    state.setdefault("rotations", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    state_path().write_text(json.dumps(state, indent=1))


def prune(state: dict[str, Any]) -> dict[str, Any]:
    """Drop expired locks and earmarks so the file can't grow without bound."""
    now = _now()
    locks: dict[str, Any] = {}
    for key, lock in (state.get("locks") or {}).items():
        keep = {
            field: raw
            for field, raw in lock.items()
            if (_parse(raw) or now) > now
        }
        if keep:
            locks[key] = keep
    state["locks"] = locks
    state["earmarks"] = [
        e for e in (state.get("earmarks") or []) if (_parse(e.get("expires")) or now) > now
    ]
    pending = state.get("pending")
    if pending and (_parse(pending.get("expires")) or now) <= now:
        state.pop("pending", None)
    cutoff = (now - timedelta(days=7)).date().isoformat()
    state["rotations"] = {
        day: n for day, n in (state.get("rotations") or {}).items() if day >= cutoff
    }
    return state


def locked(state: dict[str, Any], exchange_id: str, action: str) -> bool:
    """True when this market may not be traded in this direction yet."""
    lock = (state.get("locks") or {}).get(str(exchange_id)) or {}
    until = _parse(lock.get(f"no_{action}_until"))
    return until is not None and until > _now()


def _lock(state: dict[str, Any], exchange_id: str, action: str, hours: float) -> None:
    locks = state.setdefault("locks", {})
    entry = locks.setdefault(str(exchange_id), {})
    entry[f"no_{action}_until"] = (_now() + timedelta(hours=hours)).isoformat()


def rotations_today(state: dict[str, Any]) -> int:
    return int((state.get("rotations") or {}).get(_now().date().isoformat(), 0))


def earmarked_targets(state: dict[str, Any]) -> set[str]:
    return {str(e.get("target")) for e in (state.get("earmarks") or [])}


def _held_edge(idea: TradeIdea | None, side: str) -> float:
    """
    How much edge the model still sees in a position we already hold.

    An idea only counts if it says to keep buying this same side; anything else
    means the thesis has weakened, so the position is fair game to rotate out of.
    """
    if idea is None:
        return 0.0
    if idea.side == side and idea.action == "buy":
        return float(idea.net_edge)
    return 0.0


def rotation_proposals(
    positions_payload: dict[str, Any],
    ideas: list[TradeIdea],
    cfg: Settings,
    *,
    equity: float,
    state: dict[str, Any] | None = None,
) -> tuple[list[OrderProposal], dict[str, Any]]:
    """
    Sell proposals that free cash for a materially better idea.

    Returns the proposals plus the updated state, which the caller must persist
    once the sells are actually sent.
    """
    state = prune(state if state is not None else load_state())
    if not cfg.rotation_enabled or equity <= 0:
        return [], state

    if rotations_today(state) >= int(cfg.rotation_max_per_day):
        log.info("Rotation budget for today is used up")
        return [], state
    if state.get("earmarks") or state.get("pending"):
        # One rotation in flight at a time: wait for the sell (pending) or
        # the earmarked buy to land before starting another.
        return [], state

    idea_by_ex: dict[str, TradeIdea] = {i.exchange_id: i for i in ideas}
    held: dict[str, dict[str, Any]] = {}
    race_notional: dict[str, float] = {}
    held_races: set[str] = set()
    for pos in positions_payload.get("positions") or []:
        if pos.get("settled"):
            continue
        qty = float(pos.get("quantity") or 0)
        if qty == 0:
            continue
        exchange_id = str(pos.get("exchangeId"))
        held[exchange_id] = pos
        title = str(pos.get("marketTitle") or exchange_id)
        race = race_key_from_title(title)
        held_races.add(race)
        side = "yes" if qty > 0 else "no"
        mark = float(pos.get("currentPrice") or 0)
        own_mark = (1.0 - mark) if side == "no" else mark
        race_notional[race] = race_notional.get(race, 0.0) + abs(qty) * max(own_mark, 0.0)

    # Best idea in a race we don't already hold (party mirrors count as one race)
    candidates = [
        i
        for i in ideas
        if i.action == "buy"
        and float(i.net_edge) >= float(cfg.min_edge)
        and str(i.exchange_id) not in held
        and (i.race_key or race_key_from_title(i.market_title)) not in held_races
        and not locked(state, i.exchange_id, "buy")
    ]
    if not candidates:
        return [], state
    target = max(candidates, key=lambda i: float(i.net_edge))

    # Weakest position we're allowed to sell that's actually worth selling.
    # Prefer races already over the exposure cap, then weakest edge, then size.
    min_qty = max(1, int(cfg.bot_min_order_qty))
    max_notional = float(equity) * float(cfg.rotation_max_frac_per_trade)
    race_cap = float(cfg.max_race_exposure_frac) * float(equity)
    sellable: list[tuple[float, float, float, dict[str, Any], str, int]] = []
    for exchange_id, pos in held.items():
        if locked(state, exchange_id, "sell"):
            continue
        qty = float(pos.get("quantity") or 0)
        abs_qty = int(abs(qty))
        side = "yes" if qty > 0 else "no"
        mark = float(pos.get("currentPrice") or 0)
        own_mark = (1.0 - mark) if side == "no" else mark
        if own_mark <= 0:
            continue
        sell_qty = min(abs_qty, int(max_notional // own_mark))
        if sell_qty < min_qty:
            continue
        edge = _held_edge(idea_by_ex.get(exchange_id), side)
        race = race_key_from_title(str(pos.get("marketTitle") or exchange_id))
        # Negative when over cap so min() picks overweight races first
        over = min(0.0, race_cap - float(race_notional.get(race, 0.0)))
        sellable.append((over, edge, -(sell_qty * own_mark), pos, side, sell_qty))
    if not sellable:
        return [], state
    _, worst_edge, _, worst_pos, worst_side, sell_qty = min(
        sellable, key=lambda t: (t[0], t[1], t[2])
    )

    gain = float(target.net_edge) - worst_edge
    if gain < float(cfg.rotation_min_edge_gain):
        return [], state

    title = str(worst_pos.get("marketTitle") or worst_pos.get("exchangeId"))
    exchange_id = str(worst_pos.get("exchangeId"))
    # Earmark + daily count commit only after this sell fills (see record_fills).
    # Reserving them here burned budget when the executor skipped the sell.
    proposal = OrderProposal(
        bot="risk_manager",
        exchange_id=exchange_id,
        market_id=str(worst_pos.get("marketId") or ""),
        market_title=title,
        side=worst_side,
        action="sell",
        quantity=sell_qty,
        price=exit_limit_price(worst_pos.get("currentPrice"), worst_side),
        reason=(
            f"Rotate: {target.market_title[:36]} edge {target.net_edge:.1%} beats "
            f"this {worst_edge:.1%} by {gain:.1%}"
        ),
        priority=750,
        race_key=race_key_from_title(title),
        tags=[
            "risk",
            "rotation",
            f"earmark:{target.exchange_id}",
            f"earmark_side:{target.side}",
        ],
    )
    # Soft reservation so the next cycle does not stack another sell before
    # this one fills. Count + real earmark commit only in record_fills.
    state["pending"] = {
        "funded_by": exchange_id,
        "target": str(target.exchange_id),
        "side": target.side,
        "expires": (_now() + timedelta(minutes=float(cfg.rotation_earmark_minutes))).isoformat(),
    }
    log.info(
        "Rotation: sell %s x%s to fund %s (edge %.1f%% vs %.1f%%)",
        title[:40],
        sell_qty,
        target.market_title[:40],
        float(target.net_edge) * 100,
        worst_edge * 100,
    )
    return [proposal], state


def filter_buys(
    proposals: list[OrderProposal],
    cfg: Settings,
    *,
    state: dict[str, Any] | None = None,
) -> list[OrderProposal]:
    """
    Drop buys that would restart a loop.

    While a rotation is in flight, the freed cash belongs to that target alone;
    otherwise the next-best idea would spend it and we'd be back to churning.
    """
    if not cfg.rotation_enabled:
        return proposals
    state = prune(state if state is not None else load_state())
    targets = earmarked_targets(state)
    pending = state.get("pending") or {}
    if pending.get("target"):
        targets = targets | {str(pending["target"])}
    out: list[OrderProposal] = []
    for p in proposals:
        if p.action != "buy":
            out.append(p)
            continue
        if locked(state, p.exchange_id, "buy"):
            log.info("Skip buy — cooling down after a sell: %s", p.market_title[:50])
            continue
        if targets and str(p.exchange_id) not in targets:
            log.info("Skip buy — cash is earmarked for a rotation: %s", p.market_title[:50])
            continue
        out.append(p)
    return out


def rollback_failed_rotations(
    state: dict[str, Any],
    *,
    proposed_sell_ids: set[str],
    executed: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Clear a pending rotation when its sell never filled.

    The proposal path only soft-reserves via `pending`. Daily count and the
    cash earmark commit on a successful sell; a skip/reject must drop pending
    so the next cycle can try again instead of stalling for 20 minutes.
    """
    state = prune(state)
    pending = state.get("pending") or {}
    funded = str(pending.get("funded_by") or "")
    if not funded:
        return state
    if proposed_sell_ids and funded not in proposed_sell_ids:
        return state
    filled_sells = {
        str(row.get("exchange_id") or "")
        for row in executed
        if row.get("ok") and row.get("action") == "sell"
    }
    if funded in filled_sells:
        return state
    log.warning("Rotation sell did not fill — clearing pending for %s", funded)
    state.pop("pending", None)
    # Drop any orphan earmark left by older builds that reserved before fill
    state["earmarks"] = [
        e for e in (state.get("earmarks") or []) if str(e.get("funded_by")) != funded
    ]
    return state


def record_fills(
    executed: list[dict[str, Any]],
    cfg: Settings,
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Apply the one invariant that makes churn structurally impossible:
    **a market we just sold cannot be bought again until its cooldown expires.**

    A loop needs sell -> buy -> sell. Blocking the re-entry breaks the cycle at
    its first step, while leaving exits unrestricted so risk management still
    works whenever a thesis really does break.

    Rotation earmarks and the daily count also land here — only on a successful
    sell — so a skipped/rejected sell cannot reserve cash or burn the budget.
    """
    state = prune(state if state is not None else load_state())
    hours = float(cfg.rotation_cooldown_hours)
    bought: set[str] = set()
    for row in executed:
        if not row.get("ok"):
            continue
        exchange_id = str(row.get("exchange_id") or "")
        if not exchange_id:
            continue
        if row.get("action") == "sell":
            _lock(state, exchange_id, "buy", hours)
            tags = [str(t) for t in (row.get("tags") or [])]
            if "rotation" in tags:
                target = next(
                    (
                        t.split(":", 1)[1]
                        for t in tags
                        if t.startswith("earmark:") and not t.startswith("earmark_side:")
                    ),
                    "",
                )
                side = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("earmark_side:")),
                    "yes",
                )
                if not target:
                    pending = state.get("pending") or {}
                    if str(pending.get("funded_by")) == exchange_id:
                        target = str(pending.get("target") or "")
                        side = str(pending.get("side") or side)
                if target:
                    state.setdefault("earmarks", []).append(
                        {
                            "target": target,
                            "side": side,
                            "funded_by": exchange_id,
                            "created": _now().isoformat(),
                            "expires": (
                                _now()
                                + timedelta(minutes=float(cfg.rotation_earmark_minutes))
                            ).isoformat(),
                        }
                    )
                    day = _now().date().isoformat()
                    state.setdefault("rotations", {})[day] = rotations_today(state) + 1
                if str((state.get("pending") or {}).get("funded_by")) == exchange_id:
                    state.pop("pending", None)
        else:
            bought.add(exchange_id)

    remaining = []
    for mark in state.get("earmarks") or []:
        if str(mark.get("target")) in bought:
            log.info("Rotation complete: bought earmarked %s", mark.get("target"))
            continue
        remaining.append(mark)
    state["earmarks"] = remaining
    return state
