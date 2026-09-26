from __future__ import annotations

from typing import Any

from .edge import basket_from_title, race_key_from_title


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


def exposure_by_basket(positions_payload: dict[str, Any]) -> dict[str, float]:
    """Basket (house/senate/gov/other) → absolute notional held."""
    out: dict[str, float] = {}
    for pos in positions_payload.get("positions") or []:
        if pos.get("settled"):
            continue
        title = str(pos.get("marketTitle") or pos.get("title") or "")
        basket = basket_from_title(title) if title else "other"
        out[basket] = out.get(basket, 0.0) + position_notional(pos)
    return out


def total_exposure(positions_payload: dict[str, Any]) -> float:
    return sum(exposure_by_race(positions_payload).values())


def basket_cap_frac(basket: str, cfg: Any) -> float:
    """Max bankroll fraction allowed in a correlated basket."""
    mapping = {
        "house": float(getattr(cfg, "max_house_basket_frac", 0.55)),
        "senate": float(getattr(cfg, "max_senate_basket_frac", 0.40)),
        "gov": float(getattr(cfg, "max_gov_basket_frac", 0.30)),
        "other": float(getattr(cfg, "max_other_basket_frac", 0.25)),
    }
    return mapping.get(basket, mapping["other"])


def cap_buy_quantity(
    *,
    quantity: int,
    price: float | None,
    race_key: str,
    bankroll: float,
    exposure: dict[str, float],
    max_race_frac: float,
    max_position_frac: float,
    basket: str | None = None,
    basket_exposure: dict[str, float] | None = None,
    max_basket_frac: float | None = None,
) -> int:
    """
    Shrink a BUY so race, ticket, and optional basket exposure stay within caps.
    Sells are left unchanged by callers.
    """
    if quantity <= 0 or bankroll <= 0:
        return 0
    px = float(price) if price is not None and price > 0 else 0.5
    race_used = float(exposure.get(race_key, 0.0))
    race_room = max(0.0, bankroll * max_race_frac - race_used)
    ticket_cap = bankroll * max_position_frac
    room = min(race_room, ticket_cap)

    if basket and basket_exposure is not None and max_basket_frac is not None:
        basket_used = float(basket_exposure.get(basket, 0.0))
        basket_room = max(0.0, bankroll * max_basket_frac - basket_used)
        room = min(room, basket_room)

    max_shares = int(room // px) if px > 0 else 0
    return max(0, min(quantity, max_shares))
