from __future__ import annotations

from ..config import Settings, settings
from ..edge import race_key_from_title
from ..models import TradeIdea
from .types import OrderProposal


def _flb_allowed(cfg: Settings) -> bool:
    if cfg.can_trade_live:
        return cfg.allow_flb_live
    return cfg.allow_flb_paper


def buy_price_in_band(price: float | None, cfg: Settings) -> bool:
    """True when a buy price sits in the configured tournament payoff band."""
    if price is None:
        return False
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    return float(cfg.buy_min_price) <= px <= float(cfg.buy_max_price)


def proposals_from_edge_ideas(
    ideas: list[TradeIdea],
    *,
    bot: str = "edge_hunter",
    max_ideas: int | None = None,
    cfg: Settings | None = None,
) -> list[OrderProposal]:
    cfg = cfg or settings
    if max_ideas is None:
        max_ideas = int(cfg.bot_max_edge_ideas)
    allow_flb = _flb_allowed(cfg)
    out: list[OrderProposal] = []
    for idea in ideas:
        if idea.source == "constraint":
            continue
        if idea.source == "flb" and not allow_flb:
            continue
        if idea.action == "buy" and not buy_price_in_band(idea.market_price, cfg):
            continue
        out.append(
            OrderProposal(
                bot=bot,
                exchange_id=idea.exchange_id,
                market_id=idea.market_id,
                market_title=idea.market_title,
                side=idea.side,
                action=idea.action,
                quantity=idea.size,
                price=idea.market_price,
                reason=idea.rationale,
                priority=idea.score,
                race_key=idea.race_key or race_key_from_title(idea.market_title),
                tags=list(idea.tags) + ["edge"],
            )
        )
        if len(out) >= max_ideas:
            break
    return out


def proposals_from_constraints(
    ideas: list[TradeIdea],
    *,
    bot: str = "constraint_arb",
    max_ideas: int = 5,
) -> list[OrderProposal]:
    out: list[OrderProposal] = []
    constraint_ideas = [i for i in ideas if i.source == "constraint"]
    for idea in constraint_ideas[:max_ideas]:
        out.append(
            OrderProposal(
                bot=bot,
                exchange_id=idea.exchange_id,
                market_id=idea.market_id,
                market_title=idea.market_title,
                side=idea.side,
                action=idea.action,
                quantity=max(1, idea.size),  # full sized — structural edges are high leverage
                price=idea.market_price,
                reason=idea.rationale,
                priority=idea.score + 1_000,  # structural edges first
                race_key=idea.race_key or race_key_from_title(idea.market_title),
                tags=list(idea.tags) + ["constraint"],
            )
        )
    return out
