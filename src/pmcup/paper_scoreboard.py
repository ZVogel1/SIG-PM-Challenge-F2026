from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def ledger_path() -> Path:
    path = Path("data/bots")
    path.mkdir(parents=True, exist_ok=True)
    return path / "paper_ledger.json"


def _load() -> dict[str, Any]:
    path = ledger_path()
    if not path.exists():
        return {"fills": [], "updated_at": None}
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {"fills": [], "updated_at": None}


def _save(data: dict[str, Any]) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    ledger_path().write_text(json.dumps(data, indent=2))


def record_paper_fills(decision_rows: list[dict[str, Any]]) -> None:
    """Append successful dry-run buys/sells into the paper ledger."""
    data = _load()
    fills: list[dict[str, Any]] = list(data.get("fills") or [])
    for row in decision_rows:
        if row.get("live") or not row.get("ok"):
            continue
        if row.get("action") not in {"buy", "sell"}:
            continue
        fills.append(
            {
                "ts": row.get("ts"),
                "bot": row.get("bot"),
                "title": row.get("title"),
                "side": row.get("side"),
                "action": row.get("action"),
                "qty": row.get("qty"),
                "price": row.get("price"),
                "exchange_id": (row.get("response") or {}).get("would_post", {}).get(
                    "exchangeId"
                ),
            }
        )
    # Keep last 2k fills
    data["fills"] = fills[-2000:]
    _save(data)


def summarize_paper(
    *,
    marks_by_exchange: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Rough paper PnL from dry-run fills.
    Buys add long inventory; sells reduce it. Mark vs entry when marks provided.
    """
    data = _load()
    fills = data.get("fills") or []
    inventory: dict[str, dict[str, float]] = {}
    # key = exchange|side
    realized = 0.0
    for f in fills:
        ex = str(f.get("exchange_id") or f.get("title") or "unknown")
        side = str(f.get("side") or "yes").lower()
        key = f"{ex}|{side}"
        qty = float(f.get("qty") or 0)
        px = float(f.get("price") or 0)
        if qty <= 0 or px <= 0:
            continue
        slot = inventory.setdefault(key, {"qty": 0.0, "cost": 0.0})
        if f.get("action") == "buy":
            slot["cost"] += qty * px
            slot["qty"] += qty
        else:
            avg = (slot["cost"] / slot["qty"]) if slot["qty"] else px
            sell_qty = min(qty, slot["qty"]) if slot["qty"] else qty
            realized += sell_qty * (px - avg)
            slot["qty"] -= sell_qty
            slot["cost"] = avg * slot["qty"] if slot["qty"] > 0 else 0.0

    unrealized = 0.0
    open_notional = 0.0
    open_positions = 0
    marks = marks_by_exchange or {}
    for key, slot in inventory.items():
        if slot["qty"] <= 0:
            continue
        open_positions += 1
        avg = slot["cost"] / slot["qty"]
        ex = key.split("|", 1)[0]
        mark = marks.get(ex)
        if mark is None:
            mark = avg
        unrealized += slot["qty"] * (float(mark) - avg)
        open_notional += slot["qty"] * float(mark)

    return {
        "fill_count": len(fills),
        "open_lots": open_positions,
        "open_notional": round(open_notional, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized + unrealized, 2),
        "updated_at": data.get("updated_at"),
    }
