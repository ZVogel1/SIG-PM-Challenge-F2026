from __future__ import annotations

from typing import Any

from ..config import Settings
from ..edge import race_key_from_title
from ..models import TradeIdea
from .types import OrderProposal


def exit_limit_price(mark: Any, side: str) -> float | None:
    """
    Limit price for selling a position we hold, in that side's own units.

    Positions quote `currentPrice` in YES terms, so a NO exit must be priced at
    (1 - YES) or the order can never fill. Shade slightly to cross the spread;
    the executor replaces this with a real orderbook price when available.
    """
    try:
        yes_mark = float(mark)
    except (TypeError, ValueError):
        return None
    if yes_mark <= 0:
        return None
    px = (1.0 - yes_mark) if side == "no" else yes_mark
    return max(0.005, min(0.995, px - 0.01))


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

        # Take-profit only when configured below 100% — at 1.0+ we ride winners
        # for tournament variance instead of grinding half-exits every cycle.
        if cfg.bot_take_profit_frac < 1.0 and upnl_pct >= cfg.bot_take_profit_frac:
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
            elif (
                upnl_pct <= cfg.bot_underwater_exit_frac
                and idea.net_edge < cfg.min_edge
            ):
                # Soft thesis + deep red → free cash to rotate
                should_exit = True
                full_exit = False
                reason = (
                    f"Underwater {upnl_pct:.0%} with weak edge "
                    f"{idea.net_edge:.1%} — free cash to rotate"
                )
                priority = 640
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
        out.append(
            OrderProposal(
                bot="risk_manager",
                exchange_id=exchange_id,
                market_id=str(pos.get("marketId") or ""),
                market_title=title,
                side=side,
                action="sell",
                quantity=sell_qty,
                price=exit_limit_price(pos.get("currentPrice"), side),
                reason=reason,
                priority=priority,
                race_key=race_key_from_title(title),
                tags=["risk", "exit"],
            )
        )
    return out


def cash_buffer_proposals(
    positions_payload: dict[str, Any],
    *,
    cash: float,
    equity: float,
    cfg: Settings,
) -> list[OrderProposal]:
    """
    If cash is below target_cash_frac of equity, trim the worst mark-to-market
    positions (lowest uPnL%) to free dry powder for new buys.
    """
    target_frac = float(getattr(cfg, "target_cash_frac", 0.0) or 0.0)
    if target_frac <= 0 or equity <= 0:
        return []
    target_cash = equity * target_frac
    shortfall = target_cash - max(0.0, cash)
    if shortfall <= 0:
        return []

    ranked: list[tuple[float, dict[str, Any]]] = []
    for pos in positions_payload.get("positions") or []:
        if pos.get("settled"):
            continue
        qty = float(pos.get("quantity") or 0)
        if qty == 0:
            continue
        upnl_pct = float(pos.get("unrealizedPnlPct") or 0) / 100.0
        ranked.append((upnl_pct, pos))
    ranked.sort(key=lambda x: x[0])  # worst first

    out: list[OrderProposal] = []
    freed = 0.0
    # A few trims per cycle — enough to refill cash without flooding the book
    for upnl_pct, pos in ranked[:3]:
        if freed >= shortfall:
            break
        qty = float(pos.get("quantity") or 0)
        abs_qty = int(abs(qty))
        if abs_qty < 25:
            continue
        side = "yes" if qty > 0 else "no"
        # Trim half (or enough notionally) — leave some exposure
        sell_qty = max(25, abs_qty // 2)
        sell_qty = min(sell_qty, abs_qty)
        px = exit_limit_price(pos.get("currentPrice"), side)
        if px is None:
            px = exit_limit_price(pos.get("avgCost"), side) or 0.5
        notional = sell_qty * px
        title = str(pos.get("marketTitle") or pos.get("exchangeId") or "")
        out.append(
            OrderProposal(
                bot="risk_manager",
                exchange_id=str(pos.get("exchangeId")),
                market_id=str(pos.get("marketId") or ""),
                market_title=title,
                side=side,
                action="sell",
                quantity=sell_qty,
                price=px,
                reason=(
                    f"Cash buffer: cash {cash:.0f} < {target_frac:.0%} equity "
                    f"({target_cash:.0f}); trim worst uPnL {upnl_pct:.0%}"
                ),
                priority=620 + max(0.0, -upnl_pct) * 50,
                race_key=race_key_from_title(title),
                tags=["risk", "cash_buffer"],
            )
        )
        freed += notional
    return out
