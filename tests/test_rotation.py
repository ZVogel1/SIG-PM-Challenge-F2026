from __future__ import annotations

from typing import Any

import pytest

from pmcup.bots.rotation import (
    filter_buys,
    locked,
    record_fills,
    rollback_failed_rotations,
    rotation_proposals,
    rotations_today,
)
from pmcup.bots.types import OrderProposal
from pmcup.config import Settings
from pmcup.models import TradeIdea


@pytest.fixture(autouse=True)
def _tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "bots").mkdir(parents=True)


def _cfg(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "supermarket_api_key": "test",
        "rotation_enabled": True,
        "rotation_min_edge_gain": 0.10,
        "rotation_cooldown_hours": 12.0,
        "rotation_max_per_day": 6,
        "min_edge": 0.04,
        "bot_min_order_qty": 25,
    }
    base.update(over)
    return Settings(**base)


def _idea(exchange_id: str, net_edge: float, side: str = "yes", action: str = "buy") -> TradeIdea:
    return TradeIdea(
        market_id=exchange_id,
        market_title=f"Market {exchange_id}",
        exchange_id=exchange_id,
        side=side,
        action=action,
        market_price=0.4,
        fair_prob=0.5,
        edge=net_edge,
        size=100,
        stake=40.0,
        rationale="test",
        source="fair_prob",
        net_edge=net_edge,
    )


def _positions() -> dict[str, Any]:
    return {
        "positions": [
            {
                "exchangeId": "100",
                "marketId": "m100",
                "marketTitle": "Tired position",
                "quantity": 10_000,
                "currentPrice": 0.40,
                "settled": False,
            }
        ]
    }


def test_rotates_only_when_new_edge_clearly_better() -> None:
    cfg = _cfg()
    # Held position still has 0.20 edge; candidate only 0.25 -> gain 0.05 < 0.10
    ideas = [_idea("100", 0.20), _idea("200", 0.25)]
    props, _ = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    assert props == []

    # Candidate at 0.35 clears the 0.10 bar
    ideas = [_idea("100", 0.20), _idea("200", 0.35)]
    props, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    assert len(props) == 1
    assert props[0].action == "sell" and props[0].exchange_id == "100"
    assert "Rotate" in props[0].reason


def test_sold_market_cannot_be_bought_back() -> None:
    cfg = _cfg()
    ideas = [_idea("100", 0.20), _idea("200", 0.35)]
    props, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    # The lock lands when the sell actually fills, not when it's proposed
    state = record_fills([_fill(props[0])], cfg, state=state)
    assert locked(state, "100", "buy")

    rebuy = OrderProposal(
        bot="edge_hunter", exchange_id="100", market_id="m100",
        market_title="Tired position", side="yes", action="buy",
        quantity=5_000, price=0.4, reason="edge", priority=500,
        race_key="r", tags=[],
    )
    assert filter_buys([rebuy], cfg, state=state) == []


def test_freed_cash_is_earmarked_for_the_target_only() -> None:
    cfg = _cfg()
    ideas = [_idea("100", 0.20), _idea("200", 0.35)]
    props, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    state = record_fills([_fill(props[0])], cfg, state=state)

    def buy(ex: str) -> OrderProposal:
        return OrderProposal(
            bot="edge_hunter", exchange_id=ex, market_id=ex, market_title=f"M{ex}",
            side="yes", action="buy", quantity=1_000, price=0.3, reason="edge",
            priority=500, race_key=ex, tags=[],
        )

    kept = filter_buys([buy("200"), buy("300")], cfg, state=state)
    assert [p.exchange_id for p in kept] == ["200"]


def test_exits_are_never_blocked() -> None:
    """
    Risk exits must stay available: a loop needs sell -> buy -> sell, and the
    re-entry is already blocked, so there's no reason to trap us in a position.
    """
    cfg = _cfg()
    ideas = [_idea("100", 0.20), _idea("200", 0.35)]
    props, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    state = record_fills([_fill(props[0]), _fill(_buy("200"))], cfg, state=state)
    assert not locked(state, "200", "sell")
    assert not locked(state, "100", "sell")


def test_one_rotation_in_flight_at_a_time() -> None:
    cfg = _cfg()
    ideas = [_idea("100", 0.20), _idea("200", 0.35)]
    _, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    # Pending sell still open -> no second rotation
    again, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000, state=state)
    assert again == []


def test_daily_rotation_budget_is_enforced() -> None:
    cfg = _cfg(rotation_max_per_day=2, rotation_cooldown_hours=0.0)
    state = None
    fired = 0
    for round_ in range(6):
        positions = {
            "positions": [
                {
                    "exchangeId": f"{100 + round_}",
                    "marketId": "m",
                    "marketTitle": "Tired",
                    "quantity": 10_000,
                    "currentPrice": 0.40,
                    "settled": False,
                }
            ]
        }
        ideas = [_idea(f"{100 + round_}", 0.05), _idea(f"{900 + round_}", 0.40)]
        props, state = rotation_proposals(positions, ideas, cfg, equity=100_000, state=state)
        if props:
            fired += 1
            # sell fills (counts against budget) then buy clears the earmark
            state = record_fills(
                [_fill(props[0]), _fill(_buy(f"{900 + round_}"))],
                cfg,
                state=state,
            )
    assert fired == 2
    assert rotations_today(state) == 2


def _buy(ex: str, qty: int = 1_000, price: float = 0.3) -> OrderProposal:
    return OrderProposal(
        bot="edge_hunter", exchange_id=ex, market_id=ex, market_title=f"M{ex}",
        side="yes", action="buy", quantity=qty, price=price, reason="edge",
        priority=500, race_key=ex, tags=[],
    )


def _fill(p: OrderProposal) -> dict[str, Any]:
    return {
        "ok": True,
        "exchange_id": p.exchange_id,
        "action": p.action,
        "qty": p.quantity,
        "tags": list(p.tags or []),
    }


def test_sold_market_is_locked_out_after_any_sell() -> None:
    """A take-profit or stop sell must also block re-entry, not just rotations."""
    cfg = _cfg()
    state = record_fills(
        [{"ok": True, "exchange_id": "777", "action": "sell", "qty": 100}], cfg
    )
    assert locked(state, "777", "buy")
    assert filter_buys([_buy("777")], cfg, state=state) == []


def test_many_cycles_cannot_churn() -> None:
    """
    The failure this module exists to prevent: a static view of the world the
    bot keeps acting on, selling and re-buying forever. Cash is modelled here,
    so this measures real turnover rather than just filter behaviour.
    """
    cfg = _cfg(rotation_max_per_day=3, rotation_max_frac_per_trade=0.04)
    positions = _positions()
    ideas = [_idea("100", 0.05), _idea("200", 0.40)]
    state = None
    cash = 0.0
    turnover = 0.0
    orders = 0
    sold: set[str] = set()
    bought: set[str] = set()

    for _ in range(500):
        props, state = rotation_proposals(positions, ideas, cfg, equity=100_000, state=state)
        fills: list[dict[str, Any]] = []
        for sell in props:
            cash += sell.quantity * (sell.price or 0.4)
            turnover += sell.quantity * (sell.price or 0.4)
            orders += 1
            sold.add(sell.exchange_id)
            fills.append(_fill(sell))
        # Commit locks/earmarks from sells before the buyer acts
        state = record_fills(fills, cfg, state=state)
        fills = []

        # The buyer always wants both markets; only cash and the filters stop it
        for want in filter_buys([_buy("100"), _buy("200")], cfg, state=state):
            cost = want.quantity * (want.price or 0.3)
            if cost > cash:
                continue
            cash -= cost
            turnover += cost
            orders += 1
            bought.add(want.exchange_id)
            fills.append(_fill(want))
        state = record_fills(fills, cfg, state=state)

    assert locked(state, "100", "buy"), "sold market was not locked out"
    # The churn signature: a market traded in both directions
    assert not (sold & bought), f"round-tripped {sold & bought}"
    # Hard ceiling: each rotation can move max_frac of equity out and back in
    ceiling = 100_000 * cfg.rotation_max_frac_per_trade * cfg.rotation_max_per_day * 2
    assert turnover <= ceiling, f"turnover {turnover:,.0f} over ceiling {ceiling:,.0f}"
    assert orders < 500, f"traded on most cycles: {orders}"


def test_disabled_rotation_is_inert() -> None:
    cfg = _cfg(rotation_enabled=False)
    ideas = [_idea("100", 0.20), _idea("200", 0.90)]
    props, _ = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    assert props == []


def test_dust_position_does_not_block_a_real_rotation() -> None:
    """A tiny holding has the weakest edge but frees no cash — skip past it."""
    cfg = _cfg()
    positions = {
        "positions": [
            {
                "exchangeId": "dust", "marketId": "d", "marketTitle": "Dust",
                "quantity": 11, "currentPrice": 0.40, "settled": False,
            },
            {
                "exchangeId": "100", "marketId": "m100", "marketTitle": "Real position",
                "quantity": 10_000, "currentPrice": 0.40, "settled": False,
            },
        ]
    }
    # Both have zero model edge; only the real one can fund anything
    props, _ = rotation_proposals(positions, [_idea("200", 0.30)], cfg, equity=100_000)
    assert len(props) == 1
    assert props[0].exchange_id == "100"


def test_no_rotation_without_a_fundable_candidate() -> None:
    cfg = _cfg()
    # Only idea is the market we already hold
    props, _ = rotation_proposals(_positions(), [_idea("100", 0.50)], cfg, equity=100_000)
    assert props == []


def test_sell_size_capped_by_equity_fraction() -> None:
    cfg = _cfg(rotation_max_frac_per_trade=0.01)
    ideas = [_idea("100", 0.0), _idea("200", 0.40)]
    props, _ = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    # 1% of 100k = 1,000 notional at a 0.40 mark -> ~2,500 shares, never above
    assert 2_490 <= props[0].quantity <= 2_500
    assert props[0].quantity * 0.40 <= 1_000


def test_failed_rotation_sell_clears_pending_without_burning_budget() -> None:
    cfg = _cfg()
    ideas = [_idea("100", 0.0), _idea("200", 0.40)]
    props, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    assert state.get("pending")
    assert rotations_today(state) == 0
    state = rollback_failed_rotations(
        state,
        proposed_sell_ids={props[0].exchange_id},
        executed=[],  # executor skipped the sell entirely
    )
    assert state.get("pending") is None
    assert rotations_today(state) == 0
    assert state["earmarks"] == []


def test_resting_rotation_sell_keeps_pending() -> None:
    cfg = _cfg()
    ideas = [_idea("100", 0.0), _idea("200", 0.40)]
    props, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    state = rollback_failed_rotations(
        state,
        proposed_sell_ids={props[0].exchange_id},
        executed=[],
        resting_sell_ids={"100"},
    )
    assert state.get("pending") is not None


def test_budget_increments_only_after_rotation_sell_fills() -> None:
    cfg = _cfg()
    ideas = [_idea("100", 0.0), _idea("200", 0.40)]
    props, state = rotation_proposals(_positions(), ideas, cfg, equity=100_000)
    assert rotations_today(state) == 0
    state = record_fills([_fill(props[0])], cfg, state=state)
    assert rotations_today(state) == 1
    assert state["earmarks"][0]["target"] == "200"
    assert state.get("pending") is None


def test_does_not_rotate_into_a_race_already_held() -> None:
    """Selling Dem PA-08 to buy Rep PA-08 is the same race, not diversification."""
    cfg = _cfg()
    positions = {
        "positions": [
            {
                "exchangeId": "dem",
                "marketId": "m",
                "marketTitle": "Will the Democratic Party win the PA-08 House race?",
                "quantity": -10_000,
                "currentPrice": 0.30,
                "settled": False,
            }
        ]
    }
    ideas = [
        TradeIdea(
            market_id="rep",
            market_title="Will the Republican Party win the PA-08 House race?",
            exchange_id="rep",
            side="yes",
            action="buy",
            market_price=0.3,
            fair_prob=0.55,
            edge=0.25,
            size=100,
            stake=30.0,
            rationale="test",
            source="fair_prob",
            net_edge=0.25,
            race_key="pa-08 house race",
        ),
        _idea("other", 0.20),
    ]
    # Only "other" is a new race; 0.20 vs held edge 0 is not enough gain (need 0.10
    # against a zero-edge hold — wait, gain would be 0.20). Give held a strong edge
    # via a same-side idea so we only care about candidate filtering.
    ideas_hold = [
        TradeIdea(
            market_id="dem",
            market_title="Will the Democratic Party win the PA-08 House race?",
            exchange_id="dem",
            side="no",
            action="buy",
            market_price=0.3,
            fair_prob=0.55,
            edge=0.05,
            size=100,
            stake=30.0,
            rationale="test",
            source="fair_prob",
            net_edge=0.05,
            race_key="pa-08 house race",
        ),
        ideas[0],
        TradeIdea(
            market_id="other",
            market_title="Will the Republican Party win the CO-08 House race?",
            exchange_id="other",
            side="yes",
            action="buy",
            market_price=0.2,
            fair_prob=0.45,
            edge=0.25,
            size=100,
            stake=20.0,
            rationale="test",
            source="fair_prob",
            net_edge=0.25,
            race_key="co-08 house race",
        ),
    ]
    props, state = rotation_proposals(positions, ideas_hold, cfg, equity=100_000)
    assert len(props) == 1
    state = record_fills([_fill(props[0])], cfg, state=state)
    # Earmark must fund the new race, not the PA-08 party mirror
    assert [e["target"] for e in state["earmarks"]] == ["other"]


def test_prefers_selling_an_over_cap_race() -> None:
    cfg = _cfg(max_race_exposure_frac=0.10, rotation_max_frac_per_trade=0.04)
    positions = {
        "positions": [
            {
                "exchangeId": "fat",
                "marketId": "m1",
                "marketTitle": "Will the Republican Party win the Alaska Senate?",
                "quantity": 50_000,
                "currentPrice": 0.40,
                "settled": False,
            },
            {
                "exchangeId": "thin",
                "marketId": "m2",
                "marketTitle": "Will the Republican Party win the Maine Senate?",
                "quantity": 5_000,
                "currentPrice": 0.40,
                "settled": False,
            },
        ]
    }
    # Both holds have zero remaining edge; over-cap Alaska should be sold first
    ideas = [_idea("fresh", 0.30)]
    props, _ = rotation_proposals(positions, ideas, cfg, equity=100_000)
    assert props[0].exchange_id == "fat"
