from __future__ import annotations

from pmcup.bots.risk import risk_exit_proposals
from pmcup.bots.strategies import buy_price_in_band, proposals_from_edge_ideas
from pmcup.config import Settings
from pmcup.models import TradeIdea


def _cfg(**over) -> Settings:
    base = {
        "supermarket_api_key": "test",
        "buy_min_price": 0.12,
        "buy_max_price": 0.65,
        "bot_take_profit_frac": 0.25,
        "bot_underwater_exit_frac": -0.20,
    }
    base.update(over)
    return Settings(**base)


def _idea(price: float, *, action: str = "buy", source: str = "fair_prob") -> TradeIdea:
    return TradeIdea(
        market_id="m1",
        market_title="Will the Republican Party win the Maine Senate?",
        exchange_id="e1",
        side="yes",
        action=action,
        market_price=price,
        fair_prob=0.55,
        edge=0.15,
        size=100,
        stake=40.0,
        rationale="test",
        source=source,
        net_edge=0.12,
        score=10.0,
        race_key="maine senate",
    )


def test_buy_price_band_helper() -> None:
    cfg = _cfg()
    assert buy_price_in_band(0.12, cfg)
    assert buy_price_in_band(0.40, cfg)
    assert buy_price_in_band(0.65, cfg)
    assert not buy_price_in_band(0.04, cfg)
    assert not buy_price_in_band(0.875, cfg)
    assert not buy_price_in_band(None, cfg)


def test_edge_hunter_skips_pennies_and_favorites() -> None:
    cfg = _cfg()
    props = proposals_from_edge_ideas(
        [_idea(0.04), _idea(0.40), _idea(0.875)],
        cfg=cfg,
    )
    assert len(props) == 1
    assert props[0].price == 0.40


def test_take_profit_fires_at_25_percent() -> None:
    cfg = _cfg(bot_take_profit_frac=0.25)
    positions = {
        "positions": [
            {
                "exchangeId": "e1",
                "marketId": "m1",
                "marketTitle": "Will the Republican Party win the Maine Senate?",
                "quantity": 1_000,
                "currentPrice": 0.50,
                "unrealizedPnlPct": 30.0,  # API reports percent points
                "settled": False,
            }
        ]
    }
    props = risk_exit_proposals(positions, [], cfg)
    assert len(props) == 1
    assert props[0].action == "sell"
    assert "Take profit" in props[0].reason


def test_take_profit_does_not_fire_below_threshold() -> None:
    cfg = _cfg(bot_take_profit_frac=0.25)
    positions = {
        "positions": [
            {
                "exchangeId": "e1",
                "marketId": "m1",
                "marketTitle": "Will the Republican Party win the Maine Senate?",
                "quantity": 1_000,
                "currentPrice": 0.50,
                "unrealizedPnlPct": 20.0,
                "settled": False,
            }
        ]
    }
    assert risk_exit_proposals(positions, [], cfg) == []
