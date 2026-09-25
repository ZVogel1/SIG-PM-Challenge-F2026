from __future__ import annotations

from pmcup.config import Settings
from pmcup.models import TradeIdea
from pmcup.bots.strategies import proposals_from_edge_ideas


def _idea(source: str) -> TradeIdea:
    return TradeIdea(
        market_id="m1",
        market_title="Will the Democratic Party win the Maine Senate?",
        exchange_id="e1",
        side="yes",
        action="buy",
        market_price=0.4,
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


def test_flb_blocked_when_live() -> None:
    cfg = Settings(
        dry_run=False,
        live_trading=True,
        enforce_trading_window=False,
        allow_flb_live=False,
        allow_flb_paper=True,
    )
    props = proposals_from_edge_ideas([_idea("flb"), _idea("fair_prob")], cfg=cfg)
    assert len(props) == 1
    assert props[0].tags  # fair_prob path kept


def test_flb_allowed_in_paper() -> None:
    cfg = Settings(
        dry_run=True,
        live_trading=False,
        allow_flb_paper=True,
    )
    props = proposals_from_edge_ideas([_idea("flb")], cfg=cfg)
    assert len(props) == 1
