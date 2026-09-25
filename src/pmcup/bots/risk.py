from __future__ import annotations

from typing import Any

from ..config import Settings
from ..edge import race_key_from_title
from ..models import TradeIdea
from .types import OrderProposal


def risk_exit_proposals(
    positions_payload: dict[str, Any],
    ideas: list[TradeIdea],
    cfg: Settings,
) -> list[OrderProposal]:
    """
    Flatten or reduce positions when:
    - forecast edge has disappeared / flipped,
    - unrealized gain is large enough to bank for tournament variance,
    - underwater with no active idea (illiquid / unscanned holding).
    """
    idea_by_ex: dict[str, TradeIdea] = {i.exchange_id: i for i in ideas}
    out: list[OrderProposal] = []

    for pos in positions_payload.get("positions") or []:
        if pos.get("settled"):
            continue
        qty = float(pos.get("quantity") or 0)
        if qty == 0:
            continue
        # Platform: positive qty = YES, negative = NO
        side = "yes" if qty > 0 else "no"
        abs_qty = int(abs(qty))
        if abs_qty <= 0:
            continue

        exchange_id = str(pos.get("exchangeId"))
        title = str(pos.get("marketTitle") or exchange_id)
        upnl_pct = float(pos.get("unrealizedPnlPct") or 0) / 100.0
        idea = idea_by_ex.get(exchange_id)

        should_exit = False
        reason = ""
        priority = 0.0
        full_exit = False

        if upnl_pct >= cfg.bot_take_profit_frac:
            should_exit = True
            reason = f"Take profit: unrealized {upnl_pct:.0%} >= {cfg.bot_take_profit_frac:.0%}"
            priority = 900 + upnl_pct * 100
        elif idea is not None:
            # If we hold YES but best idea is now BUY NO (or edge gone), exit
            same_side_edge = idea.side == side and idea.action == "buy"
            if not same_side_edge and idea.net_edge >= cfg.min_edge:
                should_exit = True
                full_exit = True
                reason = (
                    f"Edge flipped against us (now {idea.action} {idea.side}, "
                    f"net {idea.net_edge:.1%})"
                )
                priority = 800 + idea.net_edge * 100
            elif same_side_edge and idea.net_edge < cfg.bot_exit_net_edge:
                should_exit = True
                full_exit = True
                reason = f"Edge decayed to {idea.net_edge:.1%} < exit bar"
                priority = 700
        elif upnl_pct <= cfg.bot_underwater_exit_frac:
            should_exit = True
            full_exit = True
            reason = (
                f"No scan idea and underwater {upnl_pct:.0%} "
                f"<= {cfg.bot_underwater_exit_frac:.0%}"
            )
            priority = 650

        if not should_exit:
            continue

        sell_qty = abs_qty if full_exit else max(1, abs_qty // 2)
        sell_qty = min(sell_qty, abs_qty)
        mark = pos.get("currentPrice")
        out.append(
            OrderProposal(
                bot="risk_manager",
                exchange_id=exchange_id,
                market_id=str(pos.get("marketId") or ""),
                market_title=title,
                side=side,
                action="sell",
                quantity=sell_qty,
                price=float(mark) if mark is not None else None,
                reason=reason,
                priority=priority,
                race_key=race_key_from_title(title),
                tags=["risk", "exit"],
            )
        )
    return out
