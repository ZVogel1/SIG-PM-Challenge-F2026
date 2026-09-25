from __future__ import annotations

import math


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def binary_kelly(p: float, price: float) -> float:
    """
    Fraction of bankroll to bet on a YES contract priced at `price`
    when true win probability is `p`. Returns 0 if no edge.
    For a binary contract paying $1: edge / odds = (p - price) / (1 - price).
    """
    p = clamp(p)
    price = clamp(price, 0.001, 0.999)
    if p <= price:
        return 0.0
    return (p - price) / (1.0 - price)


def tournament_size(
    *,
    fair_prob: float,
    price: float,
    bankroll: float,
    kelly_mult: float = 1.75,
    max_frac: float = 0.25,
    min_edge: float = 0.04,
) -> tuple[int, float, float]:
    """
    Aggressive-but-capped sizing for winner-take-most contests.

    Returns (shares, stake_susqies, edge).
    """
    edge = fair_prob - price
    if abs(edge) < min_edge:
        return 0, 0.0, edge

    # If fair < price, prefer buying NO (or selling YES). Size on the NO side.
    if edge < 0:
        no_price = 1.0 - price
        no_fair = 1.0 - fair_prob
        kelly = binary_kelly(no_fair, no_price)
        trade_price = no_price
        edge = no_fair - no_price
    else:
        kelly = binary_kelly(fair_prob, price)
        trade_price = price

    if kelly <= 0:
        return 0, 0.0, edge

    frac = min(max_frac, kelly * kelly_mult)
    # Soft dampener so we don't blow up on 95¢ favorites with tiny absolute edge.
    confidence = min(1.0, abs(edge) / 0.12)
    frac *= confidence

    stake = bankroll * frac
    if trade_price <= 0:
        return 0, 0.0, edge
    shares = int(math.floor(stake / trade_price))
    stake = shares * trade_price
    return shares, stake, edge


def round_tick(price: float, tick: float = 0.005) -> float:
    """Round to platform tick (0.005) and clamp to valid limit range."""
    rounded = round(round(price / tick) * tick, 3)
    return clamp(rounded, 0.005, 0.995)
