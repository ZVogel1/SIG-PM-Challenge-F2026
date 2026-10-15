from __future__ import annotations

from pmcup.bots.risk import weak_lot_trim_proposals
from pmcup.config import Settings
from pmcup.models import TradeIdea


def _cfg(**over) -> Settings:
    base = {
        "supermarket_api_key": "test",
        "bot_take_profit_frac": 0.12,
        "min_edge": 0.04,
        "bot_min_order_qty": 25,
        "weak_trim_enabled": True,
        "weak_trim_cash_frac": 0.02,
        "weak_trim_max_positions": 2,
    }
    base.update(over)
    return Settings(**base)


def _idea(eid: str, *, side: str, net_edge: float) -> TradeIdea:
    return TradeIdea(
        market_id=eid,
        market_title=f"Market {eid}",
        exchange_id=eid,
        side=side,
        action="buy",
        market_price=0.4,
        fair_prob=0.5,
        edge=net_edge,
        size=100,
        stake=40.0,
        rationale="t",
        source="fair_prob",
        net_edge=net_edge,
    )


def test_weak_trim_sells_dead_lots_when_cash_starved() -> None:
    cfg = _cfg()
    positions = {
        "positions": [
            {
                "exchangeId": "dead",
                "marketId": "1",
                "marketTitle": "Will the Republican Party win the Alaska Senate?",
                "quantity": 2_000,
                "currentPrice": 0.40,
                "unrealizedPnlPct": -6.0,
                "settled": False,
            },
            {
                "exchangeId": "live",
                "marketId": "2",
                "marketTitle": "Will the Republican Party win the TX-15 House race?",
                "quantity": 2_000,
                "currentPrice": 0.40,
                "unrealizedPnlPct": -5.0,
                "settled": False,
            },
        ]
    }
    ideas = [_idea("live", side="yes", net_edge=0.20)]
    props = weak_lot_trim_proposals(
        positions, ideas, cash=0.0, equity=100_000.0, cfg=cfg
    )
    assert len(props) == 1
    assert props[0].exchange_id == "dead"
    assert props[0].action == "sell"
    assert "Weak trim" in props[0].reason


def test_weak_trim_noop_when_cash_ok() -> None:
    cfg = _cfg()
    positions = {
        "positions": [
            {
                "exchangeId": "dead",
                "marketId": "1",
                "marketTitle": "Will the Republican Party win the Alaska Senate?",
                "quantity": 2_000,
                "currentPrice": 0.40,
                "unrealizedPnlPct": -6.0,
                "settled": False,
            }
        ]
    }
    assert (
        weak_lot_trim_proposals(positions, [], cash=5_000.0, equity=100_000.0, cfg=cfg)
        == []
    )


def test_weak_trim_skips_take_profit_candidates() -> None:
    cfg = _cfg()
    positions = {
        "positions": [
            {
                "exchangeId": "win",
                "marketId": "1",
                "marketTitle": "Will the Democratic Party win the TX-23 House race?",
                "quantity": 5_000,
                "currentPrice": 0.40,
                "unrealizedPnlPct": 13.0,
                "settled": False,
            }
        ]
    }
    # 13% >= 12% TP — risk_exit owns this, weak trim must not double-sell.
    assert (
        weak_lot_trim_proposals(positions, [], cash=0.0, equity=100_000.0, cfg=cfg) == []
    )
