from __future__ import annotations

from pmcup.bots.risk import cash_buffer_proposals
from pmcup.config import Settings


def test_cash_buffer_trims_when_cash_low() -> None:
    cfg = Settings(
        supermarket_api_key="test",
        target_cash_frac=0.12,
    )
    payload = {
        "positions": [
            {
                "exchangeId": "a",
                "marketId": "1",
                "marketTitle": "Will the Republican Party win the Nevada Governor?",
                "quantity": 1000,
                "currentPrice": 0.5,
                "unrealizedPnlPct": -8.0,
            },
            {
                "exchangeId": "b",
                "marketId": "2",
                "marketTitle": "Will the Republican Party win the Alaska Senate?",
                "quantity": 1000,
                "currentPrice": 0.5,
                "unrealizedPnlPct": 10.0,
            },
        ]
    }
    # equity 100k, cash 0 → need ~12k; first trim is worst (Nevada)
    props = cash_buffer_proposals(payload, cash=0.0, equity=100_000.0, cfg=cfg)
    assert props
    assert props[0].action == "sell"
    assert "Nevada" in props[0].market_title
    assert props[0].quantity >= 25


def test_cash_buffer_noop_when_funded() -> None:
    cfg = Settings(supermarket_api_key="test", target_cash_frac=0.12)
    props = cash_buffer_proposals(
        {"positions": []},
        cash=20_000.0,
        equity=100_000.0,
        cfg=cfg,
    )
    assert props == []
