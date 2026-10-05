from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pmcup.bots.executor import Executor
from pmcup.bots.risk import exit_limit_price
from pmcup.bots.types import OrderProposal
from pmcup.config import Settings


class FakeClient:
    """Minimal stand-in that records what the executor would send."""

    def __init__(self, open_orders: list[dict[str, Any]] | None = None) -> None:
        self._open = open_orders or []
        self.placed: list[dict[str, Any]] = []
        self.canceled: list[Any] = []

    def open_orders(self, **_: Any) -> list[dict[str, Any]]:
        return self._open

    def cancel_order(self, order_id: Any) -> dict[str, Any]:
        self.canceled.append(order_id)
        return {"orderId": order_id}

    def orderbook(self, exchange_id: str, **_: Any) -> dict[str, Any]:
        return {"bestBid": 0.695, "bestAsk": 0.70}

    def account(self) -> dict[str, Any]:
        return {"balance": 50_000}

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        self.placed.append(kwargs)
        return {"ok": True}


def _cfg(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "supermarket_api_key": "test",
        "dry_run": False,
        "live_trading": True,
        "enforce_trading_window": False,
    }
    base.update(over)
    return Settings(**base)


def _positions() -> dict[str, Any]:
    return {
        "positions": [
            {
                "exchangeId": "1066",
                "marketTitle": "Will the Democratic Party win the Alaska Senate?",
                "quantity": -21326,
                "currentPrice": 0.693,
            }
        ]
    }


def _sell(qty: int = 10_000) -> OrderProposal:
    return OrderProposal(
        bot="risk_manager",
        exchange_id="1066",
        market_id="377",
        market_title="Will the Democratic Party win the Alaska Senate?",
        side="no",
        action="sell",
        quantity=qty,
        price=0.3,
        reason="test",
        priority=600,
        race_key="alaska senate",
        tags=["risk"],
    )


def test_no_side_exit_price_uses_complement() -> None:
    # Position marks at 0.693 in YES terms; a NO exit must price near 0.307
    px = exit_limit_price(0.693, "no")
    assert px is not None and 0.28 < px < 0.32
    yes_px = exit_limit_price(0.693, "yes")
    assert yes_px is not None and abs(yes_px - 0.683) < 1e-6


def test_skips_proposal_when_order_already_resting() -> None:
    client = FakeClient(
        open_orders=[
            {
                "id": 1,
                "exchangeId": "1066",
                "side": "no",
                "action": "sell",
                "quantity": 10_000,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
        ]
    )
    ex = Executor(client, _cfg(), "t1", positions_payload=_positions(), cash_available=0.0)
    results = ex.execute([_sell()])
    assert results == []
    assert client.placed == []


def test_cancels_stale_orders() -> None:
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    client = FakeClient(
        open_orders=[
            {
                "id": 7,
                "exchangeId": "1066",
                "side": "no",
                "action": "sell",
                "quantity": 10_000,
                "createdAt": old,
            }
        ]
    )
    ex = Executor(client, _cfg(), "t1", positions_payload=_positions(), cash_available=0.0)
    ex.execute([_sell()])
    assert client.canceled == [7]
    # Once the stale order is gone the fresh sell can go out
    assert len(client.placed) == 1
    assert client.placed[0]["action"] == "sell"


def test_sell_never_exceeds_held_quantity() -> None:
    client = FakeClient()
    ex = Executor(client, _cfg(), "t1", positions_payload=_positions(), cash_available=0.0)
    ex.execute([_sell(qty=5_000_000)])
    assert client.placed[0]["quantity"] == 21326


def test_no_sell_priced_off_book_complement() -> None:
    client = FakeClient()
    ex = Executor(client, _cfg(), "t1", positions_payload=_positions(), cash_available=0.0)
    ex.execute([_sell()])
    # NO sell hits the NO bid = 1 - best YES ask = 0.30
    assert client.placed[0]["price"] == 0.30
