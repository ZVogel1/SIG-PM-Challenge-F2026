from __future__ import annotations

import re

from .models import Quote, TradeIdea
from .sizing import tournament_size


def race_key_from_title(title: str) -> str:
    """Collapse Dem/Rep mirrors of the same contest into one key."""
    t = title.strip()
    t = re.sub(r"^Will the (Democratic|Republican) Party win the\s+", "", t, flags=re.I)
    t = re.sub(r"\?$", "", t).strip().lower()
    return t


def edge_from_fair(
    quote: Quote,
    fair_yes: float,
    *,
    bankroll: float,
    kelly_mult: float,
    max_frac: float,
    min_edge: float,
    confidence: float = 1.0,
    min_net_edge: float | None = None,
) -> TradeIdea | None:
    """Model-vs-market using executable prices and spread-aware net edge."""
    # Align net-edge floor with configured min_edge unless overridden
    if min_net_edge is None:
        min_net_edge = min_edge
    spread = quote.implied_spread()
    half_spread = spread / 2.0

    buy_yes = quote.buy_yes_price()
    buy_no = quote.buy_no_price()
    if buy_yes is None or buy_no is None:
        return None

    edge_yes = fair_yes - buy_yes
    edge_no = (1.0 - fair_yes) - buy_no

    # Pick the better executable side
    if edge_yes >= edge_no:
        side, action = "yes", "buy"
        trade_price = buy_yes
        fair = fair_yes
        raw_edge = edge_yes
        rationale = (
            f"Forecast YES ({fair_yes:.0%}) > executable ask ({buy_yes:.0%}). "
            "Buy YES."
        )
    else:
        side, action = "no", "buy"
        trade_price = buy_no
        fair = 1.0 - fair_yes
        raw_edge = edge_no
        rationale = (
            f"Forecast YES ({fair_yes:.0%}) < market; NO ask ({buy_no:.0%}) is cheap. "
            "Buy NO."
        )

    net_edge = raw_edge - half_spread
    if net_edge < min_net_edge or raw_edge < min_edge:
        return None

    # Size on net edge (more conservative than raw)
    effective_fair = trade_price + max(net_edge, 0.0)
    shares, stake, _ = tournament_size(
        fair_prob=effective_fair,
        price=trade_price,
        bankroll=bankroll,
        kelly_mult=kelly_mult * confidence,
        max_frac=max_frac * (0.5 + 0.5 * confidence),
        min_edge=min_net_edge,
    )
    if shares <= 0:
        return None

    score = net_edge * stake * confidence
    return TradeIdea(
        market_id=quote.market_id,
        market_title=quote.market_title,
        exchange_id=quote.exchange_id,
        side=side,
        action=action,
        market_price=trade_price,
        fair_prob=fair,
        edge=raw_edge,
        size=shares,
        stake=stake,
        rationale=rationale,
        source="fair_prob",
        tags=["model_vs_market", "spread_aware"],
        net_edge=net_edge,
        spread=spread,
        race_key=race_key_from_title(quote.market_title),
        confidence=confidence,
        score=score,
    )


def edge_from_flb(
    quote: Quote,
    *,
    bankroll: float,
    kelly_mult: float,
    max_frac: float,
    longshot_cutoff: float = 0.10,
    favorite_floor: float = 0.90,
    shrink: float = 0.35,
) -> TradeIdea | None:
    """Weak FLB prior when no forecast row exists."""
    price = quote.mid
    if price is None:
        return None

    fair: float | None = None
    tag = ""
    if price < longshot_cutoff:
        fair = max(0.005, price * (1.0 - shrink))
        tag = "fade_longshot"
    elif price > favorite_floor:
        gap = (1.0 - price) * shrink
        fair = min(0.995, price + gap)
        tag = "lean_favorite"
    else:
        return None

    idea = edge_from_fair(
        quote,
        fair,
        bankroll=bankroll,
        kelly_mult=kelly_mult * 0.5,
        max_frac=max_frac * 0.4,
        min_edge=0.025,
        confidence=0.35,
        min_net_edge=0.025,  # FLB keeps a lower bar; live bots can filter source=flb
    )
    if idea is None:
        return None
    idea.source = "flb"
    idea.tags = ["favorite_longshot_bias", tag, "spread_aware"]
    idea.rationale = f"FLB heuristic ({tag}). " + idea.rationale
    return idea


def dedupe_complement_markets(ideas: list[TradeIdea]) -> list[TradeIdea]:
    """
    Keep one trade per underlying contest.
    Dem YES and Rep NO on the same race are the same economic bet.
    """
    best: dict[str, TradeIdea] = {}
    for idea in ideas:
        key = idea.race_key or f"{idea.exchange_id}:{idea.side}"
        prev = best.get(key)
        if prev is None or idea.score > prev.score:
            best[key] = idea
    return sorted(best.values(), key=lambda x: x.score, reverse=True)
