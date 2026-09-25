from __future__ import annotations

from ..edge import race_key_from_title
from ..models import TradeIdea
from .types import OrderProposal


def proposals_from_edge_ideas(
    ideas: list[TradeIdea],
    *,
    bot: str = "edge_hunter",
    max_ideas: int = 8,
) -> list[OrderProposal]:
    out: list[OrderProposal] = []
    for idea in ideas[:max_ideas]:
        if idea.source == "constraint":
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
                quantity=max(1, idea.size // 2),
                price=idea.market_price,
                reason=idea.rationale,
                priority=idea.score + 1_000,  # structural edges first
                race_key=idea.race_key or race_key_from_title(idea.market_title),
                tags=list(idea.tags) + ["constraint"],
            )
        )
    return out
