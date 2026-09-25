from __future__ import annotations

from typing import Any

from .edge import race_key_from_title


def position_notional(pos: dict[str, Any]) -> float:
    qty = abs(float(pos.get("quantity") or 0))
    if qty <= 0:
        return 0.0
    mark = pos.get("currentPrice")
    if mark is None:
        mark = pos.get("avgCost") or pos.get("averageCost") or 0
    try:
        return qty * float(mark)
    except (TypeError, ValueError):
        return 0.0


def exposure_by_race(positions_payload: dict[str, Any]) -> dict[str, float]:
    """Race key → absolute notional SUSQies already held."""
    out: dict[str, float] = {}
    for pos in positions_payload.get("positions") or []:
        if pos.get("settled"):
            continue
        title = str(pos.get("marketTitle") or pos.get("title") or "")
        key = race_key_from_title(title) if title else str(pos.get("exchangeId") or "")
        if not key:
            continue
        out[key] = out.get(key, 0.0) + position_notional(pos)
    return out


def total_exposure(positions_payload: dict[str, Any]) -> float:
    return sum(exposure_by_race(positions_payload).values())


def cap_buy_quantity(
    *,
    quantity: int,
    price: float | None,
    race_key: str,
    bankroll: float,
    exposure: dict[str, float],
    max_race_frac: float,
    max_position_frac: float,
) -> int:
    """
    Shrink a BUY so race exposure and single-ticket size stay within caps.
    Sells are left unchanged by callers.
    """
    if quantity <= 0 or bankroll <= 0:
        return 0
    px = float(price) if price is not None and price > 0 else 0.5
    race_used = float(exposure.get(race_key, 0.0))
    race_room = max(0.0, bankroll * max_race_frac - race_used)
    ticket_cap = bankroll * max_position_frac
    room = min(race_room, ticket_cap)
    max_shares = int(room // px) if px > 0 else 0
    return max(0, min(quantity, max_shares))
