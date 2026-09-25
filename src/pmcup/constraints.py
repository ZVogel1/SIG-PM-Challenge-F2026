from __future__ import annotations

from typing import Any

from .models import TradeIdea


def ideas_from_constraints(
    payload: dict[str, Any],
    *,
    bankroll: float,
    max_frac: float = 0.15,
    default_stake_frac: float = 0.08,
) -> list[TradeIdea]:
    """
    Turn platform relationship violations into trade ideas.

    The engine already suggests corrective trades — we size them for the cup.
    This is one of the highest-leverage edges because it's structural, not opinion.
    """
    ideas: list[TradeIdea] = []
    for row in payload.get("data") or []:
        if row.get("evaluationStatus") != "violated":
            continue
        violation = float(row.get("violationAmount") or 0)
        reason = row.get("reason") or "Cross-market prices are inconsistent."
        for sug in row.get("suggestedCorrectiveTrades") or []:
            exchange_id = str(sug.get("exchangeId") or "")
            if not exchange_id:
                continue
            outcome_side = str(sug.get("outcomeSide") or "Yes").lower()
            side = "yes" if "yes" in outcome_side else "no"
            action = str(sug.get("action") or "Buy").lower()
            if action not in {"buy", "sell"}:
                action = "buy"
            price = sug.get("currentPrice")
            if price is None:
                continue
            price = float(price)
            stake = min(bankroll * max_frac, bankroll * default_stake_frac * (1 + violation * 5))
            if price <= 0:
                continue
            shares = max(1, int(stake / price))
            stake = shares * price
            title = sug.get("marketTitle") or f"exchange {exchange_id}"
            ideas.append(
                TradeIdea(
                    market_id=str(sug.get("marketId") or ""),
                    market_title=str(title),
                    exchange_id=exchange_id,
                    side=side,
                    action=action,
                    market_price=price,
                    fair_prob=min(0.99, price + violation) if side == "yes" else min(0.99, (1 - price) + violation),
                    edge=violation,
                    size=shares,
                    stake=stake,
                    rationale=(
                        f"Platform constraint violation ({violation:.1%}): {reason} "
                        f"Suggested: {sug.get('rationale') or action.upper() + ' ' + side.upper()}."
                    ),
                    source="constraint",
                    tags=["relationship_arb", "structural"],
                )
            )
    ideas.sort(key=lambda x: abs(x.edge) * x.stake, reverse=True)
    return ideas
