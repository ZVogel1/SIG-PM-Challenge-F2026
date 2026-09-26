from __future__ import annotations

from pmcup.edge import basket_from_title, blend_fair_with_market
from pmcup.portfolio import basket_cap_frac, cap_buy_quantity, exposure_by_basket
from pmcup.config import Settings


def test_blend_high_confidence_keeps_model() -> None:
    fair, w = blend_fair_with_market(0.70, 0.50, confidence=0.95, strength=1.0)
    assert w < 0.1
    assert abs(fair - 0.70) < 0.02


def test_blend_low_confidence_pulls_to_market() -> None:
    fair, w = blend_fair_with_market(0.70, 0.50, confidence=0.40, strength=1.0)
    assert w == 0.6
    assert abs(fair - (0.4 * 0.70 + 0.6 * 0.50)) < 1e-9


def test_basket_from_title() -> None:
    assert basket_from_title("Will the Democratic Party win the TX-15 House race?") == "house"
    assert basket_from_title("Will the Republican Party win the Maine Senate?") == "senate"
    assert basket_from_title("Will the Democratic Party win the Oregon Governor?") == "gov"


def test_basket_cap_blocks_correlated_house() -> None:
    qty = cap_buy_quantity(
        quantity=50_000,
        price=0.25,
        race_key="tx-15 house race",
        bankroll=100_000,
        exposure={"tx-15 house race": 0},
        max_race_frac=0.25,
        max_position_frac=0.25,
        basket="house",
        basket_exposure={"house": 50_000},  # already 50% in house
        max_basket_frac=0.55,
    )
    # Only 5k room left in house basket → 20_000 shares at 0.25
    assert qty == 20_000


def test_basket_cap_frac_settings() -> None:
    cfg = Settings(max_house_basket_frac=0.5, max_senate_basket_frac=0.3)
    assert basket_cap_frac("house", cfg) == 0.5
    assert basket_cap_frac("senate", cfg) == 0.3


def test_exposure_by_basket() -> None:
    payload = {
        "positions": [
            {
                "marketTitle": "Will the Democratic Party win the TX-15 House race?",
                "quantity": 100,
                "currentPrice": 0.5,
            },
            {
                "marketTitle": "Will the Republican Party win the IA-01 House race?",
                "quantity": 200,
                "currentPrice": 0.25,
            },
            {
                "marketTitle": "Will the Democratic Party win the Maine Senate?",
                "quantity": 50,
                "currentPrice": 0.4,
            },
        ]
    }
    exp = exposure_by_basket(payload)
    assert abs(exp["house"] - (50 + 50)) < 1e-6  # 100*0.5 + 200*0.25
    assert abs(exp["senate"] - 20) < 1e-6
