from __future__ import annotations

from pmcup.portfolio import (
    cap_buy_quantity,
    cap_buy_to_cash,
    cash_and_equity,
    exposure_by_race,
)


def test_exposure_by_race_sums_mirrors() -> None:
    payload = {
        "positions": [
            {
                "marketTitle": "Will the Democratic Party win the Maine Senate?",
                "quantity": 100,
                "currentPrice": 0.5,
            },
            {
                "marketTitle": "Will the Republican Party win the Maine Senate?",
                "quantity": -50,
                "currentPrice": 0.4,
            },
        ]
    }
    exp = exposure_by_race(payload)
    assert "maine senate" in exp
    assert abs(exp["maine senate"] - (100 * 0.5 + 50 * 0.4)) < 1e-6


def test_cap_buy_quantity_blocks_over_race_cap() -> None:
    qty = cap_buy_quantity(
        quantity=10_000,
        price=0.5,
        race_key="maine senate",
        bankroll=100_000,
        exposure={"maine senate": 24_000},
        max_race_frac=0.25,
        max_position_frac=0.25,
    )
    # Only ~1000 SUSQies room → 2000 shares at 0.5
    assert qty == 2000


def test_cap_buy_quantity_zero_when_full() -> None:
    qty = cap_buy_quantity(
        quantity=500,
        price=0.4,
        race_key="x",
        bankroll=100_000,
        exposure={"x": 25_000},
        max_race_frac=0.25,
        max_position_frac=0.25,
    )
    assert qty == 0


def test_cash_and_equity_uses_marks() -> None:
    cash, equity = cash_and_equity(
        {"balance": 40_000},
        {"summary": {"totalMarketValue": 90_000}},
    )
    assert cash == 40_000
    assert equity == 130_000


def test_cap_buy_to_cash() -> None:
    assert cap_buy_to_cash(10_000, 0.5, 100) == 200
    assert cap_buy_to_cash(10, 0.5, 0) == 0
