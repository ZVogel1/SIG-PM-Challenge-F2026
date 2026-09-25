from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import TradeIdea


def paper_path() -> Path:
    path = Path("data/paper")
    path.mkdir(parents=True, exist_ok=True)
    return path / "portfolio.json"


def load_portfolio(starting_cash: float = 100_000.0) -> dict[str, Any]:
    path = paper_path()
    if path.exists():
        return json.loads(path.read_text())
    return {
        "cash": starting_cash,
        "starting_cash": starting_cash,
        "positions": {},  # exchange_id -> {side, qty, avg_price, title, race_key}
        "fills": [],
        "updated_at": None,
    }


def save_portfolio(portfolio: dict[str, Any]) -> Path:
    portfolio["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = paper_path()
    path.write_text(json.dumps(portfolio, indent=2))
    return path


def apply_ideas(
    ideas: list[TradeIdea],
    *,
    top_n: int = 5,
    starting_cash: float = 100_000.0,
) -> dict[str, Any]:
    """
    Paper-fill top ideas at executable prices (no live orders).
    Used to practice / stress-test sizing before Oct 1.
    """
    portfolio = load_portfolio(starting_cash)
    cash = float(portfolio["cash"])
    positions = portfolio.get("positions") or {}
    fills = portfolio.get("fills") or []

    applied = []
    for idea in ideas[:top_n]:
        cost = idea.stake
        if cost > cash:
            # shrink to remaining cash
            if idea.market_price <= 0:
                continue
            qty = int(cash // idea.market_price)
            if qty <= 0:
                continue
            cost = qty * idea.market_price
            size = qty
        else:
            size = idea.size

        key = f"{idea.exchange_id}:{idea.side}"
        prev = positions.get(key)
        if prev:
            new_qty = prev["qty"] + size
            new_avg = ((prev["avg_price"] * prev["qty"]) + cost) / new_qty
            prev.update({"qty": new_qty, "avg_price": new_avg})
        else:
            positions[key] = {
                "exchange_id": idea.exchange_id,
                "market_id": idea.market_id,
                "title": idea.market_title,
                "side": idea.side,
                "qty": size,
                "avg_price": idea.market_price,
                "race_key": idea.race_key,
                "fair_prob_at_entry": idea.fair_prob,
                "net_edge_at_entry": idea.net_edge,
            }
        cash -= cost
        fill = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "exchange_id": idea.exchange_id,
            "title": idea.market_title,
            "side": idea.side,
            "action": "buy",
            "qty": size,
            "price": idea.market_price,
            "cost": cost,
            "net_edge": idea.net_edge,
            "source": idea.source,
        }
        fills.append(fill)
        applied.append(fill)

    portfolio["cash"] = round(cash, 2)
    portfolio["positions"] = positions
    portfolio["fills"] = fills
    path = save_portfolio(portfolio)
    return {"portfolio_path": str(path), "applied": applied, "cash": cash, "position_count": len(positions)}


def mark_portfolio(ideas_by_exchange_side: dict[tuple[str, str], TradeIdea] | None = None) -> dict[str, Any]:
    """Rough MTM using latest idea market prices when available."""
    portfolio = load_portfolio()
    positions = portfolio.get("positions") or {}
    mtm = float(portfolio["cash"])
    details = []
    for key, pos in positions.items():
        mark = pos["avg_price"]
        if ideas_by_exchange_side:
            idea = ideas_by_exchange_side.get((pos["exchange_id"], pos["side"]))
            if idea:
                mark = idea.market_price
        value = pos["qty"] * mark
        mtm += value
        details.append({**pos, "mark": mark, "value": value})
    return {
        "cash": portfolio["cash"],
        "mtm": round(mtm, 2),
        "pnl": round(mtm - float(portfolio["starting_cash"]), 2),
        "positions": details,
    }
